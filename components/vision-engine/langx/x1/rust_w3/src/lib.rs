#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(i32)]
pub enum X1Error {
    Dim = -1,
    Stride = -2,
    Short = -3,
    Grid = -4,
    Rect = -5,
}

impl std::fmt::Display for X1Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            X1Error::Dim => write!(f, "E_DIM"),
            X1Error::Stride => write!(f, "E_STRIDE"),
            X1Error::Short => write!(f, "E_SHORT"),
            X1Error::Grid => write!(f, "E_GRID"),
            X1Error::Rect => write!(f, "E_RECT"),
        }
    }
}

impl std::error::Error for X1Error {}

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

    if src_stride < 4 * w_usize || dst_stride < 3 * w_usize {
        return Err(X1Error::Stride);
    }

    if w == 0 || h == 0 {
        return Ok(());
    }

    let min_src_len = (h_usize - 1) * src_stride + 4 * w_usize;
    let min_dst_len = (h_usize - 1) * dst_stride + 3 * w_usize;

    if src.len() < min_src_len || dst.len() < min_dst_len {
        return Err(X1Error::Short);
    }

    for y in 0..h_usize {
        let src_row = &src[y * src_stride..y * src_stride + 4 * w_usize];
        let dst_row = &mut dst[y * dst_stride..y * dst_stride + 3 * w_usize];

        for (src_pixel, dst_pixel) in src_row.chunks_exact(4).zip(dst_row.chunks_exact_mut(3)) {
            // src_pixel layout: [B, G, R, X]
            // dst_pixel layout: [R, G, B]
            dst_pixel[0] = src_pixel[2];
            dst_pixel[1] = src_pixel[1];
            dst_pixel[2] = src_pixel[0];
        }
    }

    Ok(())
}

pub fn damage_tiles(
    w: u32,
    h: u32,
    s: u32,
    rects: &[(i32, i32, i32, i32)],
) -> Result<(Vec<u64>, u32), X1Error> {
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

    let s_u64 = s as u64;
    let w_u64 = w as u64;
    let h_u64 = h as u64;

    let gw = (w_u64 + s_u64 - 1) / s_u64;
    let gh = (h_u64 + s_u64 - 1) / s_u64;
    let total_tiles = gw * gh;
    let num_words = ((total_tiles + 63) / 64) as usize;

    let mut words = vec![0u64; num_words];

    for &(x, y, rw, rh) in rects {
        let x0 = (x as i64).max(0);
        let y0 = (y as i64).max(0);
        let x1 = ((x as i64) + (rw as i64)).min(w as i64);
        let y1 = ((y as i64) + (rh as i64)).min(h as i64);

        if x1 <= x0 || y1 <= y0 {
            continue;
        }

        let s_i64 = s as i64;
        let tx_start = (x0 / s_i64) as u64;
        let tx_end = ((x1 + s_i64 - 1) / s_i64) as u64;
        let ty_start = (y0 / s_i64) as u64;
        let ty_end = ((y1 + s_i64 - 1) / s_i64) as u64;

        for ty in ty_start..ty_end {
            for tx in tx_start..tx_end {
                let t = ty * gw + tx;
                let word_idx = (t / 64) as usize;
                let bit_idx = (t % 64) as u32;
                words[word_idx] |= 1u64 << bit_idx;
            }
        }
    }

    let popcount: u32 = words.iter().map(|w| w.count_ones()).sum();

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

pub fn fnv1a_64(data: &[u8]) -> u64 {
    let mut hash: u64 = 0xcbf29ce484222325;
    let prime: u64 = 0x100000001b3;
    for &byte in data {
        hash = (hash ^ (byte as u64)).wrapping_mul(prime);
    }
    hash
}

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
        return X1Error::Short as i32;
    }
    let src_slice = std::slice::from_raw_parts(src, src_len);
    let dst_slice = std::slice::from_raw_parts_mut(dst, dst_len);

    match repack_bgrx_to_rgb(src_slice, w, h, src_stride, dst_slice, dst_stride) {
        Ok(()) => 0,
        Err(err) => err as i32,
    }
}

#[no_mangle]
pub unsafe extern "C" fn x1_damage(
    w: u32,
    h: u32,
    s: u32,
    rects: *const i32,
    num_rects: usize,
    dst_words: *mut u64,
    dst_words_len: usize,
    out_popcount: *mut u32,
) -> i32 {
    let rect_slice = if num_rects == 0 {
        &[]
    } else {
        if rects.is_null() {
            return X1Error::Short as i32;
        }
        std::slice::from_raw_parts(rects as *const (i32, i32, i32, i32), num_rects)
    };

    match damage_tiles(w, h, s, rect_slice) {
        Ok((words, popcount)) => {
            if dst_words.is_null() && !words.is_empty() {
                return X1Error::Short as i32;
            }
            if dst_words_len < words.len() {
                return X1Error::Short as i32;
            }
            if !words.is_empty() {
                let out_slice = std::slice::from_raw_parts_mut(dst_words, dst_words_len);
                out_slice[..words.len()].copy_from_slice(&words);
            }
            if !out_popcount.is_null() {
                *out_popcount = popcount;
            }
            0
        }
        Err(err) => err as i32,
    }
}
