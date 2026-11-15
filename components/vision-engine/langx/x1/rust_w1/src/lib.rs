use std::error::Error;
use std::fmt;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum X1Error {
    EDim,
    EStride,
    EShort,
    ERect,
    EGrid,
}

impl fmt::Display for X1Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            X1Error::EDim => write!(f, "E_DIM"),
            X1Error::EStride => write!(f, "E_STRIDE"),
            X1Error::EShort => write!(f, "E_SHORT"),
            X1Error::ERect => write!(f, "E_RECT"),
            X1Error::EGrid => write!(f, "E_GRID"),
        }
    }
}

impl Error for X1Error {}

#[repr(C)]
#[derive(Debug, Clone, Copy)]
pub struct X1Rect {
    pub x: i32,
    pub y: i32,
    pub w: i32,
    pub h: i32,
}

pub fn repack_bgrx_to_rgb(
    src: &[u8],
    w: i32,
    h: i32,
    src_stride: usize,
    dst: &mut [u8],
    dst_stride: usize,
) -> Result<(), X1Error> {
    if w < 0 || w > 16384 || h < 0 || h > 16384 {
        return Err(X1Error::EDim);
    }

    let w_usize = w as usize;
    let h_usize = h as usize;

    let min_src_stride = w_usize.checked_mul(4).ok_or(X1Error::EStride)?;
    let min_dst_stride = w_usize.checked_mul(3).ok_or(X1Error::EStride)?;

    if src_stride < min_src_stride || dst_stride < min_dst_stride {
        return Err(X1Error::EStride);
    }

    let min_src_len = if h == 0 || w == 0 {
        0
    } else {
        (h_usize - 1)
            .checked_mul(src_stride)
            .and_then(|val| val.checked_add(min_src_stride))
            .ok_or(X1Error::EShort)?
    };

    let min_dst_len = if h == 0 || w == 0 {
        0
    } else {
        (h_usize - 1)
            .checked_mul(dst_stride)
            .and_then(|val| val.checked_add(min_dst_stride))
            .ok_or(X1Error::EShort)?
    };

    if src.len() < min_src_len || dst.len() < min_dst_len {
        return Err(X1Error::EShort);
    }

    if w == 0 || h == 0 {
        return Ok(());
    }

    for y in 0..h_usize {
        let src_row_start = y * src_stride;
        let dst_row_start = y * dst_stride;

        let src_row = &src[src_row_start..src_row_start + 4 * w_usize];
        let dst_row = &mut dst[dst_row_start..dst_row_start + 3 * w_usize];

        for x in 0..w_usize {
            let b = src_row[4 * x];
            let g = src_row[4 * x + 1];
            let r = src_row[4 * x + 2];

            dst_row[3 * x] = r;
            dst_row[3 * x + 1] = g;
            dst_row[3 * x + 2] = b;
        }
    }

    Ok(())
}

pub fn damage_tiles(
    w: i32,
    h: i32,
    s: i32,
    rects: &[(i32, i32, i32, i32)],
) -> Result<(Vec<u64>, usize), X1Error> {
    if w < 1 || w > 16384 || h < 1 || h > 16384 {
        return Err(X1Error::EDim);
    }

    if s < 1 || s > 256 || !(s as u32).is_power_of_two() {
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

    let gw = (w_64 + s_64 - 1) / s_64;
    let gh = (h_64 + s_64 - 1) / s_64;

    let total_tiles = (gw * gh) as usize;
    let num_words = (total_tiles + 63) / 64;

    let mut words = vec![0u64; num_words];

    for &(rx, ry, rw, rh) in rects {
        let x0 = (rx as i64).max(0);
        let y0 = (ry as i64).max(0);
        let x1 = ((rx as i64) + (rw as i64)).min(w_64);
        let y1 = ((ry as i64) + (rh as i64)).min(h_64);

        if x1 <= x0 || y1 <= y0 {
            continue;
        }

        let tx0 = x0 / s_64;
        let ty0 = y0 / s_64;
        let tx1 = (x1 + s_64 - 1) / s_64;
        let ty1 = (y1 + s_64 - 1) / s_64;

        for ty in ty0..ty1 {
            let row_offset = ty * gw;
            for tx in tx0..tx1 {
                let t = (row_offset + tx) as usize;
                let word_idx = t / 64;
                let bit_idx = t % 64;
                words[word_idx] |= 1u64 << bit_idx;
            }
        }
    }

    let popcount = words.iter().map(|w| w.count_ones() as usize).sum();
    Ok((words, popcount))
}

pub struct SplitMix64 {
    state: u64,
}

impl SplitMix64 {
    pub fn new(seed: u64) -> Self {
        Self { state: seed }
    }

    pub fn next_u64(&mut self) -> u64 {
        self.state = self.state.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.state;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
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

pub fn fnv1a_64(data: &[u8]) -> u64 {
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    const PRIME: u64 = 0x0100_0000_01b3;
    for &byte in data {
        hash ^= byte as u64;
        hash = hash.wrapping_mul(PRIME);
    }
    hash
}

/// C ABI function for repack_bgrx_to_rgb
#[no_mangle]
pub unsafe extern "C" fn x1_repack(
    src_ptr: *const u8,
    src_len: usize,
    w: i32,
    h: i32,
    src_stride: usize,
    dst_ptr: *mut u8,
    dst_len: usize,
    dst_stride: usize,
) -> i32 {
    // SAFETY: Constructing slice views over supplied pointers within given lengths if non-null.
    if (src_len > 0 && src_ptr.is_null()) || (dst_len > 0 && dst_ptr.is_null()) {
        return -3; // E_SHORT
    }

    let src = if src_len == 0 || src_ptr.is_null() {
        &[]
    } else {
        std::slice::from_raw_parts(src_ptr, src_len)
    };

    let dst = if dst_len == 0 || dst_ptr.is_null() {
        &mut []
    } else {
        std::slice::from_raw_parts_mut(dst_ptr, dst_len)
    };

    match repack_bgrx_to_rgb(src, w, h, src_stride, dst, dst_stride) {
        Ok(()) => 0,
        Err(X1Error::EDim) => -1,
        Err(X1Error::EStride) => -2,
        Err(X1Error::EShort) => -3,
        Err(X1Error::ERect) => -4,
        Err(X1Error::EGrid) => -5,
    }
}

/// C ABI function for damage_tiles
#[no_mangle]
pub unsafe extern "C" fn x1_damage(
    w: i32,
    h: i32,
    s: i32,
    rects_ptr: *const X1Rect,
    num_rects: usize,
    words_out: *mut u64,
    words_cap: usize,
    words_written_out: *mut usize,
    popcount_out: *mut usize,
) -> i32 {
    // SAFETY: Constructing slice view over rects_ptr within num_rects if non-null.
    let rects_slice = if num_rects == 0 {
        &[]
    } else {
        if rects_ptr.is_null() {
            return -3; // E_SHORT
        }
        std::slice::from_raw_parts(rects_ptr, num_rects)
    };

    let rects_tuples: Vec<(i32, i32, i32, i32)> = rects_slice
        .iter()
        .map(|r| (r.x, r.y, r.w, r.h))
        .collect();

    match damage_tiles(w, h, s, &rects_tuples) {
        Ok((words, popcount)) => {
            if !words_written_out.is_null() {
                *words_written_out = words.len();
            }
            if !popcount_out.is_null() {
                *popcount_out = popcount;
            }
            if !words.is_empty() {
                if words_out.is_null() || words_cap < words.len() {
                    return -3; // E_SHORT
                }
                std::ptr::copy_nonoverlapping(words.as_ptr(), words_out, words.len());
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
