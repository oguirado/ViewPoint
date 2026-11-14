//! S0/S1 luma & YCoCg lifting plus S2 SAD windows with aarch64 NEON SIMD.
#![allow(clippy::too_many_arguments)]
use crate::isqrt::{active_mutant, MUT_BAND_OFF_BY_ONE, MUT_SAD_PLUS_ONE};
use crate::par::{in_bounded_pool, split_bands};
use rayon::prelude::*;

pub type Result<T> = std::result::Result<T, SadWinError>;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SadWinError {
    IndexOutOfBounds,
    DimensionMismatch,
    InvalidTileSize,
}

#[cfg(target_arch = "aarch64")]
#[inline]
unsafe fn sad_row_i16_neon(cur: &[i16], prev: &[i16]) -> i64 {
    use std::arch::aarch64::*;
    let n = cur.len();
    let mut i = 0usize;
    let mut acc = vdupq_n_s64(0);
    let c_ptr = cur.as_ptr();
    let p_ptr = prev.as_ptr();
    while i + 16 <= n {
        let c0 = unsafe { vld1q_s16(c_ptr.add(i)) };
        let p0 = unsafe { vld1q_s16(p_ptr.add(i)) };
        let d0_lo = unsafe { vabsq_s32(vsubl_s16(vget_low_s16(c0), vget_low_s16(p0))) };
        let d0_hi = unsafe { vabsq_s32(vsubl_high_s16(c0, p0)) };
        let s0 = unsafe { vaddq_s32(d0_lo, d0_hi) };

        let c1 = unsafe { vld1q_s16(c_ptr.add(i + 8)) };
        let p1 = unsafe { vld1q_s16(p_ptr.add(i + 8)) };
        let d1_lo = unsafe { vabsq_s32(vsubl_s16(vget_low_s16(c1), vget_low_s16(p1))) };
        let d1_hi = unsafe { vabsq_s32(vsubl_high_s16(c1, p1)) };
        let s1 = unsafe { vaddq_s32(d1_lo, d1_hi) };

        acc = unsafe { vpadalq_s32(acc, vaddq_s32(s0, s1)) };
        i += 16;
    }
    while i + 8 <= n {
        let c0 = unsafe { vld1q_s16(c_ptr.add(i)) };
        let p0 = unsafe { vld1q_s16(p_ptr.add(i)) };
        let d0_lo = unsafe { vabsq_s32(vsubl_s16(vget_low_s16(c0), vget_low_s16(p0))) };
        let d0_hi = unsafe { vabsq_s32(vsubl_high_s16(c0, p0)) };
        let s0 = unsafe { vaddq_s32(d0_lo, d0_hi) };
        acc = unsafe { vpadalq_s32(acc, s0) };
        i += 8;
    }
    let mut total = unsafe { vgetq_lane_s64(acc, 0) + vgetq_lane_s64(acc, 1) };
    while i < n {
        total += ((cur[i] as i64) - (prev[i] as i64)).abs();
        i += 1;
    }
    total
}

/// Fast path for full unclipped tile of width s <= 128 where s is a multiple of 16:
/// accumulates in int32x4_t across the entire tile and reduces once at the end.
#[cfg(target_arch = "aarch64")]
#[inline]
unsafe fn sad_full_tile_i16_neon(
    y_cur: &[i16],
    y_prev: &[i16],
    w: usize,
    yy: usize,
    xx: usize,
    sy: usize,
    sx: usize,
    s: usize,
) -> i64 {
    use std::arch::aarch64::*;
    let mut acc32 = vdupq_n_s32(0);
    let c_base = unsafe { y_cur.as_ptr().add(yy * w + xx) };
    let p_base = unsafe { y_prev.as_ptr().add(sy * w + sx) };
    for r in 0..s {
        let c_row = unsafe { c_base.add(r * w) };
        let p_row = unsafe { p_base.add(r * w) };
        let mut col = 0usize;
        while col + 16 <= s {
            let c0 = unsafe { vld1q_s16(c_row.add(col)) };
            let p0 = unsafe { vld1q_s16(p_row.add(col)) };
            let d0_lo = unsafe { vabsq_s32(vsubl_s16(vget_low_s16(c0), vget_low_s16(p0))) };
            let d0_hi = unsafe { vabsq_s32(vsubl_high_s16(c0, p0)) };

            let c1 = unsafe { vld1q_s16(c_row.add(col + 8)) };
            let p1 = unsafe { vld1q_s16(p_row.add(col + 8)) };
            let d1_lo = unsafe { vabsq_s32(vsubl_s16(vget_low_s16(c1), vget_low_s16(p1))) };
            let d1_hi = unsafe { vabsq_s32(vsubl_high_s16(c1, p1)) };

            let s01 = unsafe { vaddq_s32(vaddq_s32(d0_lo, d0_hi), vaddq_s32(d1_lo, d1_hi)) };
            acc32 = unsafe { vaddq_s32(acc32, s01) };
            col += 16;
        }
    }
    unsafe { vaddlvq_s32(acc32) }
}

/// Scalar twin reference implementation of `_tile_sad_windows`.
pub fn tile_sad_windows_scalar(
    y_cur: &[i16],
    y_prev: &[i16],
    h: usize,
    w: usize,
    dx: i64,
    dy: i64,
    s: usize,
    tx: &[i64],
    ty: &[i64],
) -> Result<(Vec<i64>, Vec<i64>)> {
    if s == 0 {
        return Err(SadWinError::InvalidTileSize);
    }
    if y_cur.len() != h * w || y_prev.len() != h * w || tx.len() != ty.len() {
        return Err(SadWinError::DimensionMismatch);
    }
    let w_i64 = w as i64;
    let h_i64 = h as i64;
    let s_i64 = s as i64;
    let max_tx = w_i64 / s_i64;
    let max_ty = h_i64 / s_i64;

    for i in 0..tx.len() {
        let x = tx[i];
        let y = ty[i];
        if x < -max_tx || x >= max_tx || y < -max_ty || y >= max_ty {
            return Err(SadWinError::IndexOutOfBounds);
        }
    }

    let n_req = tx.len();
    let mut sad = vec![0i64; n_req];
    let mut count = vec![0i64; n_req];

    for i in 0..n_req {
        let mut x = tx[i];
        let mut y = ty[i];
        if x < 0 {
            x += max_tx;
        }
        if y < 0 {
            y += max_ty;
        }
        let yy = y * s_i64;
        let xx = x * s_i64;
        let y0 = yy.max(dy);
        let y1 = (yy + s_i64).min(h_i64 + dy);
        let x0 = xx.max(dx);
        let x1 = (xx + s_i64).min(w_i64 + dx);

        if y0 < y1 && x0 < x1 {
            let mut tile_sad: i64 = 0;
            for r in y0..y1 {
                let cur_row_off = (r as usize) * w;
                let prev_row_off = ((r - dy) as usize) * w;
                for c in x0..x1 {
                    let cur_px = y_cur[cur_row_off + (c as usize)] as i64;
                    let prev_px = y_prev[prev_row_off + ((c - dx) as usize)] as i64;
                    tile_sad += (cur_px - prev_px).abs();
                }
            }
            sad[i] = tile_sad;
            count[i] = (y1 - y0) * (x1 - x0);
        }
    }
    Ok((sad, count))
}

#[allow(clippy::too_many_arguments)]
fn sad_windows_band_i16(
    y_cur: &[i16],
    y_prev: &[i16],
    w: usize,
    w_i64: i64,
    h_i64: i64,
    dx: i64,
    dy: i64,
    s: usize,
    s_i64: i64,
    max_tx: i64,
    max_ty: i64,
    full_fast: bool,
    mutant: u8,
    tx: &[i64],
    ty: &[i64],
    sad_out: &mut [i64],
    count_out: &mut [i64],
) {
    for i in 0..tx.len() {
        let mut x = tx[i];
        let mut y = ty[i];
        if x < 0 {
            x += max_tx;
        }
        if y < 0 {
            y += max_ty;
        }
        let yy = y * s_i64;
        let xx = x * s_i64;
        let y0 = yy.max(dy);
        let y1 = (yy + s_i64).min(h_i64 + dy);
        let x0 = xx.max(dx);
        let x1 = (xx + s_i64).min(w_i64 + dx);

        let mut tile_sad: i64 = 0;
        let mut tile_cnt: i64 = 0;
        if y0 < y1 && x0 < x1 {
            let ny = y1 - y0;
            let nx = x1 - x0;
            tile_cnt = ny * nx;
            if full_fast && ny == s_i64 && nx == s_i64 {
                #[cfg(target_arch = "aarch64")]
                {
                    tile_sad = unsafe {
                        sad_full_tile_i16_neon(
                            y_cur,
                            y_prev,
                            w,
                            y0 as usize,
                            x0 as usize,
                            (y0 - dy) as usize,
                            (x0 - dx) as usize,
                            s,
                        )
                    };
                }
                #[cfg(not(target_arch = "aarch64"))]
                {
                    let len = nx as usize;
                    for r in y0..y1 {
                        let c_off = (r as usize) * w + (x0 as usize);
                        let p_off = ((r - dy) as usize) * w + ((x0 - dx) as usize);
                        for k in 0..len {
                            tile_sad +=
                                ((y_cur[c_off + k] as i64) - (y_prev[p_off + k] as i64)).abs();
                        }
                    }
                }
            } else {
                let len = nx as usize;
                for r in y0..y1 {
                    let c_off = (r as usize) * w + (x0 as usize);
                    let p_off = ((r - dy) as usize) * w + ((x0 - dx) as usize);
                    let cur_slice = &y_cur[c_off..c_off + len];
                    let prev_slice = &y_prev[p_off..p_off + len];
                    #[cfg(target_arch = "aarch64")]
                    {
                        tile_sad += unsafe { sad_row_i16_neon(cur_slice, prev_slice) };
                    }
                    #[cfg(not(target_arch = "aarch64"))]
                    {
                        for k in 0..len {
                            tile_sad += ((cur_slice[k] as i64) - (prev_slice[k] as i64)).abs();
                        }
                    }
                }
            }
        }
        if mutant == MUT_SAD_PLUS_ONE {
            tile_sad += 1; // MUTANT:SAD_PLUS_ONE: tile_sad += 1;
        }
        sad_out[i] = tile_sad;
        count_out[i] = tile_cnt;
    }
}

/// Write SIMD SAD window results directly into caller-provided `sad_out` and `count_out` slices.
pub fn tile_sad_windows_into(
    y_cur: &[i16],
    y_prev: &[i16],
    h: usize,
    w: usize,
    dx: i64,
    dy: i64,
    s: usize,
    tx: &[i64],
    ty: &[i64],
    sad_out: &mut [i64],
    count_out: &mut [i64],
) -> Result<()> {
    if s == 0 {
        return Err(SadWinError::InvalidTileSize);
    }
    if y_cur.len() != h * w
        || y_prev.len() != h * w
        || tx.len() != ty.len()
        || sad_out.len() != tx.len()
        || count_out.len() != tx.len()
    {
        return Err(SadWinError::DimensionMismatch);
    }

    let w_i64 = w as i64;
    let h_i64 = h as i64;
    let s_i64 = s as i64;
    let max_tx = w_i64 / s_i64;
    let max_ty = h_i64 / s_i64;

    for i in 0..tx.len() {
        let x = tx[i];
        let y = ty[i];
        if x < -max_tx || x >= max_tx || y < -max_ty || y >= max_ty {
            return Err(SadWinError::IndexOutOfBounds);
        }
    }

    let total = tx.len();
    if total == 0 {
        return Ok(());
    }

    let mutant = active_mutant();
    if mutant == MUT_BAND_OFF_BY_ONE {
        sad_out.fill(-1);
        count_out.fill(-1);
    }
    let full_fast = s <= 128 && (s & 15) == 0;

    in_bounded_pool(|n_threads| {
        let bands = split_bands(total, n_threads);
        if n_threads <= 1 || bands.len() <= 1 {
            for (start, end) in bands {
                sad_windows_band_i16(
                    y_cur,
                    y_prev,
                    w,
                    w_i64,
                    h_i64,
                    dx,
                    dy,
                    s,
                    s_i64,
                    max_tx,
                    max_ty,
                    full_fast,
                    mutant,
                    &tx[start..end],
                    &ty[start..end],
                    &mut sad_out[start..end],
                    &mut count_out[start..end],
                );
            }
        } else {
            let mut tasks = Vec::with_capacity(bands.len());
            let mut sad_rem = sad_out;
            let mut cnt_rem = count_out;
            let mut prev_end = 0usize;
            for (start, end) in bands {
                let skip = start - prev_end;
                let len = end - start;
                if skip > 0 {
                    sad_rem = &mut sad_rem[skip..];
                    cnt_rem = &mut cnt_rem[skip..];
                }
                let (s_slice, s_rest) = sad_rem.split_at_mut(len);
                let (c_slice, c_rest) = cnt_rem.split_at_mut(len);
                sad_rem = s_rest;
                cnt_rem = c_rest;
                prev_end = end;
                tasks.push((&tx[start..end], &ty[start..end], s_slice, c_slice));
            }
            tasks
                .into_par_iter()
                .with_max_len(1)
                .for_each(|(tx_b, ty_b, sad_b, cnt_b)| {
                    sad_windows_band_i16(
                        y_cur, y_prev, w, w_i64, h_i64, dx, dy, s, s_i64, max_tx, max_ty,
                        full_fast, mutant, tx_b, ty_b, sad_b, cnt_b,
                    );
                });
        }
    });
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn sad_windows_band_i64(
    y_cur: &[i64],
    y_prev: &[i64],
    w: usize,
    w_i64: i64,
    h_i64: i64,
    dx: i64,
    dy: i64,
    s_i64: i64,
    max_tx: i64,
    max_ty: i64,
    mutant: u8,
    tx: &[i64],
    ty: &[i64],
    sad_out: &mut [i64],
    count_out: &mut [i64],
) {
    for i in 0..tx.len() {
        let mut x = tx[i];
        let mut y = ty[i];
        if x < 0 {
            x += max_tx;
        }
        if y < 0 {
            y += max_ty;
        }
        let yy = y * s_i64;
        let xx = x * s_i64;
        let y0 = yy.max(dy);
        let y1 = (yy + s_i64).min(h_i64 + dy);
        let x0 = xx.max(dx);
        let x1 = (xx + s_i64).min(w_i64 + dx);

        let mut tile_sad: i64 = 0;
        let mut tile_cnt: i64 = 0;
        if y0 < y1 && x0 < x1 {
            tile_cnt = (y1 - y0) * (x1 - x0);
            for r in y0..y1 {
                let c_off = (r as usize) * w;
                let p_off = ((r - dy) as usize) * w;
                for c in x0..x1 {
                    let cur_px = y_cur[c_off + (c as usize)];
                    let prev_px = y_prev[p_off + ((c - dx) as usize)];
                    tile_sad = tile_sad.wrapping_add(cur_px.wrapping_sub(prev_px).wrapping_abs());
                }
            }
        }
        if mutant == MUT_SAD_PLUS_ONE {
            tile_sad += 1;
        }
        sad_out[i] = tile_sad;
        count_out[i] = tile_cnt;
    }
}

pub fn tile_sad_windows_i64_into(
    y_cur: &[i64],
    y_prev: &[i64],
    h: usize,
    w: usize,
    dx: i64,
    dy: i64,
    s: usize,
    tx: &[i64],
    ty: &[i64],
    sad_out: &mut [i64],
    count_out: &mut [i64],
) -> Result<()> {
    if s == 0 {
        return Err(SadWinError::InvalidTileSize);
    }
    if y_cur.len() != h * w
        || y_prev.len() != h * w
        || tx.len() != ty.len()
        || sad_out.len() != tx.len()
        || count_out.len() != tx.len()
    {
        return Err(SadWinError::DimensionMismatch);
    }
    let w_i64 = w as i64;
    let h_i64 = h as i64;
    let s_i64 = s as i64;
    let max_tx = w_i64 / s_i64;
    let max_ty = h_i64 / s_i64;

    for i in 0..tx.len() {
        let x = tx[i];
        let y = ty[i];
        if x < -max_tx || x >= max_tx || y < -max_ty || y >= max_ty {
            return Err(SadWinError::IndexOutOfBounds);
        }
    }

    let total = tx.len();
    if total == 0 {
        return Ok(());
    }

    let mutant = active_mutant();
    if mutant == MUT_BAND_OFF_BY_ONE {
        sad_out.fill(-1);
        count_out.fill(-1);
    }

    in_bounded_pool(|n_threads| {
        let bands = split_bands(total, n_threads);
        if n_threads <= 1 || bands.len() <= 1 {
            for (start, end) in bands {
                sad_windows_band_i64(
                    y_cur,
                    y_prev,
                    w,
                    w_i64,
                    h_i64,
                    dx,
                    dy,
                    s_i64,
                    max_tx,
                    max_ty,
                    mutant,
                    &tx[start..end],
                    &ty[start..end],
                    &mut sad_out[start..end],
                    &mut count_out[start..end],
                );
            }
        } else {
            let mut tasks = Vec::with_capacity(bands.len());
            let mut sad_rem = sad_out;
            let mut cnt_rem = count_out;
            let mut prev_end = 0usize;
            for (start, end) in bands {
                let skip = start - prev_end;
                let len = end - start;
                if skip > 0 {
                    sad_rem = &mut sad_rem[skip..];
                    cnt_rem = &mut cnt_rem[skip..];
                }
                let (s_slice, s_rest) = sad_rem.split_at_mut(len);
                let (c_slice, c_rest) = cnt_rem.split_at_mut(len);
                sad_rem = s_rest;
                cnt_rem = c_rest;
                prev_end = end;
                tasks.push((&tx[start..end], &ty[start..end], s_slice, c_slice));
            }
            tasks
                .into_par_iter()
                .with_max_len(1)
                .for_each(|(tx_b, ty_b, sad_b, cnt_b)| {
                    sad_windows_band_i64(
                        y_cur, y_prev, w, w_i64, h_i64, dx, dy, s_i64, max_tx, max_ty, mutant,
                        tx_b, ty_b, sad_b, cnt_b,
                    );
                });
        }
    });
    Ok(())
}

pub fn tile_sad_windows(
    y_cur: &[i16],
    y_prev: &[i16],
    h: usize,
    w: usize,
    dx: i64,
    dy: i64,
    s: usize,
    tx: &[i64],
    ty: &[i64],
) -> Result<(Vec<i64>, Vec<i64>)> {
    let mut sad = vec![0i64; tx.len()];
    let mut count = vec![0i64; tx.len()];
    tile_sad_windows_into(y_cur, y_prev, h, w, dx, dy, s, tx, ty, &mut sad, &mut count)?;
    Ok((sad, count))
}

pub fn tile_sad(
    y_cur: &[i16],
    y_prev: &[i16],
    h: usize,
    w: usize,
    dx: i64,
    dy: i64,
    s: usize,
    tx: &[i64],
    ty: &[i64],
) -> Result<(Vec<i64>, Vec<bool>)> {
    let (sad, count) = tile_sad_windows(y_cur, y_prev, h, w, dx, dy, s, tx, ty)?;
    let full_cnt = (s * s) as i64;
    let valid = count.into_iter().map(|c| c == full_cnt).collect();
    Ok((sad, valid))
}

pub fn tile_sad_clip(
    y_cur: &[i16],
    y_prev: &[i16],
    h: usize,
    w: usize,
    dx: i64,
    dy: i64,
    s: usize,
    tx: &[i64],
    ty: &[i64],
) -> Result<(Vec<i64>, Vec<i64>)> {
    tile_sad_windows(y_cur, y_prev, h, w, dx, dy, s, tx, ty)
}

fn luma_band_into(padded_rgb: &[u8], out_luma: &mut [i16]) {
    let n = out_luma.len();
    assert_eq!(padded_rgb.len(), n * 3);
    let mut i = 0usize;
    #[cfg(target_arch = "aarch64")]
    {
        use std::arch::aarch64::*;
        let src_ptr = padded_rgb.as_ptr();
        let dst_ptr = out_luma.as_mut_ptr();
        while i + 16 <= n {
            unsafe {
                let rgb = vld3q_u8(src_ptr.add(i * 3));
                // t = b + ((r - b) >> 1) == (r + b) >> 1
                let t = vhaddq_u8(rgb.0, rgb.2);
                // y = t + ((g - t) >> 1) == (g + t) >> 1
                let y_u8 = vhaddq_u8(rgb.1, t);
                let y_lo = vreinterpretq_s16_u16(vmovl_u8(vget_low_u8(y_u8)));
                let y_hi = vreinterpretq_s16_u16(vmovl_high_u8(y_u8));
                vst1q_s16(dst_ptr.add(i), y_lo);
                vst1q_s16(dst_ptr.add(i + 8), y_hi);
            }
            i += 16;
        }
    }
    while i < n {
        let r = padded_rgb[i * 3] as i16;
        let g = padded_rgb[i * 3 + 1] as i16;
        let b = padded_rgb[i * 3 + 2] as i16;
        let t = b + ((r - b) >> 1);
        out_luma[i] = t + ((g - t) >> 1);
        i += 1;
    }
}

/// YCoCg-R luma plane (int16) of a padded RGB8 buffer, using NEON vld3q_u8 + vhaddq_u8 on aarch64.
pub fn luma_of_padded_into(padded_rgb: &[u8], out_luma: &mut [i16]) {
    let n = out_luma.len();
    assert_eq!(padded_rgb.len(), n * 3);
    if n == 0 {
        return;
    }
    if active_mutant() == MUT_BAND_OFF_BY_ONE {
        out_luma.fill(-1);
    }
    in_bounded_pool(|n_threads| {
        let bands = split_bands(n, n_threads);
        if n_threads <= 1 || bands.len() <= 1 {
            for (start, end) in bands {
                luma_band_into(&padded_rgb[start * 3..end * 3], &mut out_luma[start..end]);
            }
        } else {
            let mut tasks = Vec::with_capacity(bands.len());
            let mut out_rem = out_luma;
            let mut prev_end = 0usize;
            for (start, end) in bands {
                let skip = start - prev_end;
                let len = end - start;
                if skip > 0 {
                    out_rem = &mut out_rem[skip..];
                }
                let (out_slice, out_rest) = out_rem.split_at_mut(len);
                out_rem = out_rest;
                prev_end = end;
                tasks.push((&padded_rgb[start * 3..end * 3], out_slice));
            }
            tasks
                .into_par_iter()
                .with_max_len(1)
                .for_each(|(rgb_b, out_b)| {
                    luma_band_into(rgb_b, out_b);
                });
        }
    });
}

pub fn luma_of_padded(padded_rgb: &[u8], width: usize, height: usize) -> Vec<i16> {
    let mut out = vec![0i16; width * height];
    luma_of_padded_into(padded_rgb, &mut out);
    out
}

fn ycocg_band_into(rgb: &[u8], y_out: &mut [i16], co_out: &mut [i16], cg_out: &mut [i16]) {
    let n = y_out.len();
    assert_eq!(rgb.len(), n * 3);
    assert_eq!(co_out.len(), n);
    assert_eq!(cg_out.len(), n);
    let mut i = 0usize;
    #[cfg(target_arch = "aarch64")]
    {
        use std::arch::aarch64::*;
        let src_ptr = rgb.as_ptr();
        let y_ptr = y_out.as_mut_ptr();
        let co_ptr = co_out.as_mut_ptr();
        let cg_ptr = cg_out.as_mut_ptr();
        while i + 16 <= n {
            unsafe {
                let p = vld3q_u8(src_ptr.add(i * 3));
                let r_lo = vreinterpretq_s16_u16(vmovl_u8(vget_low_u8(p.0)));
                let r_hi = vreinterpretq_s16_u16(vmovl_high_u8(p.0));
                let g_lo = vreinterpretq_s16_u16(vmovl_u8(vget_low_u8(p.1)));
                let g_hi = vreinterpretq_s16_u16(vmovl_high_u8(p.1));
                let b_lo = vreinterpretq_s16_u16(vmovl_u8(vget_low_u8(p.2)));
                let b_hi = vreinterpretq_s16_u16(vmovl_high_u8(p.2));

                let co_lo = vsubq_s16(r_lo, b_lo);
                let co_hi = vsubq_s16(r_hi, b_hi);
                let t_lo = vaddq_s16(b_lo, vshrq_n_s16(co_lo, 1));
                let t_hi = vaddq_s16(b_hi, vshrq_n_s16(co_hi, 1));
                let cg_lo = vsubq_s16(g_lo, t_lo);
                let cg_hi = vsubq_s16(g_hi, t_hi);
                let y_lo = vaddq_s16(t_lo, vshrq_n_s16(cg_lo, 1));
                let y_hi = vaddq_s16(t_hi, vshrq_n_s16(cg_hi, 1));

                vst1q_s16(y_ptr.add(i), y_lo);
                vst1q_s16(y_ptr.add(i + 8), y_hi);
                vst1q_s16(co_ptr.add(i), co_lo);
                vst1q_s16(co_ptr.add(i + 8), co_hi);
                vst1q_s16(cg_ptr.add(i), cg_lo);
                vst1q_s16(cg_ptr.add(i + 8), cg_hi);
            }
            i += 16;
        }
    }
    while i < n {
        let r = rgb[i * 3] as i16;
        let g = rgb[i * 3 + 1] as i16;
        let b = rgb[i * 3 + 2] as i16;
        let co = r - b;
        let t = b + (co >> 1);
        let cg = g - t;
        let y = t + (cg >> 1);
        y_out[i] = y;
        co_out[i] = co;
        cg_out[i] = cg;
        i += 1;
    }
}

pub fn ycocg_into(rgb: &[u8], y_out: &mut [i16], co_out: &mut [i16], cg_out: &mut [i16]) {
    let n = y_out.len();
    assert_eq!(rgb.len(), n * 3);
    assert_eq!(co_out.len(), n);
    assert_eq!(cg_out.len(), n);
    if n == 0 {
        return;
    }
    if active_mutant() == MUT_BAND_OFF_BY_ONE {
        y_out.fill(-1);
        co_out.fill(-1000);
        cg_out.fill(-1000);
    }
    in_bounded_pool(|n_threads| {
        let bands = split_bands(n, n_threads);
        if n_threads <= 1 || bands.len() <= 1 {
            for (start, end) in bands {
                ycocg_band_into(
                    &rgb[start * 3..end * 3],
                    &mut y_out[start..end],
                    &mut co_out[start..end],
                    &mut cg_out[start..end],
                );
            }
        } else {
            let mut tasks = Vec::with_capacity(bands.len());
            let mut y_rem = y_out;
            let mut co_rem = co_out;
            let mut cg_rem = cg_out;
            let mut prev_end = 0usize;
            for (start, end) in bands {
                let skip = start - prev_end;
                let len = end - start;
                if skip > 0 {
                    y_rem = &mut y_rem[skip..];
                    co_rem = &mut co_rem[skip..];
                    cg_rem = &mut cg_rem[skip..];
                }
                let (y_s, y_r) = y_rem.split_at_mut(len);
                let (co_s, co_r) = co_rem.split_at_mut(len);
                let (cg_s, cg_r) = cg_rem.split_at_mut(len);
                y_rem = y_r;
                co_rem = co_r;
                cg_rem = cg_r;
                prev_end = end;
                tasks.push((&rgb[start * 3..end * 3], y_s, co_s, cg_s));
            }
            tasks
                .into_par_iter()
                .with_max_len(1)
                .for_each(|(rgb_b, y_b, co_b, cg_b)| {
                    ycocg_band_into(rgb_b, y_b, co_b, cg_b);
                });
        }
    });
}

pub fn pad_into(
    frame_rgb: &[u8],
    w: usize,
    h: usize,
    padded_w: usize,
    padded_h: usize,
    out: &mut [u8],
) {
    assert_eq!(frame_rgb.len(), w * h * 3);
    assert_eq!(out.len(), padded_w * padded_h * 3);
    if w == padded_w && h == padded_h {
        out.copy_from_slice(frame_rgb);
        return;
    }
    for y in 0..padded_h {
        let src_y = y.min(h - 1);
        let src_row = &frame_rgb[src_y * w * 3..(src_y + 1) * w * 3];
        let dst_row = &mut out[y * padded_w * 3..(y + 1) * padded_w * 3];
        dst_row[..w * 3].copy_from_slice(src_row);
        let edge_px = &src_row[(w - 1) * 3..w * 3];
        for x in w..padded_w {
            dst_row[x * 3..x * 3 + 3].copy_from_slice(edge_px);
        }
    }
}

pub fn words_eq(
    padded_cur: &[u8],
    padded_prev: &[u8],
    s: usize,
    gw: usize,
    gh: usize,
    flat: &[i64],
) -> Vec<bool> {
    let row_bytes = gw * s * 3;
    let tile_row_bytes = s * 3;
    let mut grid_eq = vec![false; gh * gw];
    for ty in 0..gh {
        for tx in 0..gw {
            let mut eq = true;
            for py in 0..s {
                let off = (ty * s + py) * row_bytes + tx * tile_row_bytes;
                if padded_cur[off..off + tile_row_bytes] != padded_prev[off..off + tile_row_bytes] {
                    eq = false;
                    break;
                }
            }
            grid_eq[ty * gw + tx] = eq;
        }
    }
    flat.iter().map(|&idx| grid_eq[idx as usize]).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::isqrt::{set_active_mutant, MUT_NONE, MUT_SAD_PLUS_ONE};

    #[test]
    fn test_sad_simd_matches_scalar_incl_extremes_and_clips() {
        let _lock = crate::isqrt::TEST_MUTEX
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        set_active_mutant(MUT_NONE);
        let h = 48usize;
        let w = 64usize;
        let s = 16usize;
        let mut y_cur = vec![0i16; h * w];
        let mut y_prev = vec![0i16; h * w];
        for i in 0..(h * w) {
            y_cur[i] = if i % 3 == 0 {
                -32768
            } else if i % 3 == 1 {
                32767
            } else {
                ((i as i32 * 73) % 511 - 255) as i16
            };
            y_prev[i] = if i % 5 == 0 {
                32767
            } else if i % 5 == 1 {
                -32768
            } else {
                ((i as i32 * 37) % 511 - 255) as i16
            };
        }
        let tx = vec![0, 3, -1, -4, 1, 2];
        let ty = vec![0, 2, -1, -3, 1, 0];
        for &(dx, dy) in &[(0, 0), (3, -2), (-16, 16), (64, 48), (-5, 7)] {
            let (s_ref, c_ref) =
                tile_sad_windows_scalar(&y_cur, &y_prev, h, w, dx, dy, s, &tx, &ty).unwrap();
            let (s_opt, c_opt) =
                tile_sad_windows(&y_cur, &y_prev, h, w, dx, dy, s, &tx, &ty).unwrap();
            assert_eq!(s_opt, s_ref);
            assert_eq!(c_opt, c_ref);
        }

        // Verify mutant SAD_PLUS_ONE is killed by comparison against scalar reference
        set_active_mutant(MUT_SAD_PLUS_ONE);
        let (s_mut, _) = tile_sad_windows(&y_cur, &y_prev, h, w, 0, 0, s, &tx, &ty).unwrap();
        let (s_ref, _) = tile_sad_windows_scalar(&y_cur, &y_prev, h, w, 0, 0, s, &tx, &ty).unwrap();
        assert_ne!(s_mut, s_ref);
        set_active_mutant(MUT_NONE);
    }

    #[test]
    fn test_luma_and_ycocg_neon_vs_scalar() {
        let _lock = crate::isqrt::TEST_MUTEX
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        set_active_mutant(MUT_NONE);
        let mut rgb = Vec::with_capacity(300 * 3);
        for i in 0..300 {
            rgb.push(((i * 17) & 255) as u8);
            rgb.push(((i * 59 + 3) & 255) as u8);
            rgb.push(((i * 101 + 7) & 255) as u8);
        }
        let luma = luma_of_padded(&rgb, 30, 10);
        let mut y_out = vec![0i16; 300];
        let mut co_out = vec![0i16; 300];
        let mut cg_out = vec![0i16; 300];
        ycocg_into(&rgb, &mut y_out, &mut co_out, &mut cg_out);
        assert_eq!(luma, y_out);
        for i in 0..300 {
            let r = rgb[i * 3] as i16;
            let g = rgb[i * 3 + 1] as i16;
            let b = rgb[i * 3 + 2] as i16;
            let co = r - b;
            let t = b + (co >> 1);
            let cg = g - t;
            let y = t + (cg >> 1);
            assert_eq!(y_out[i], y);
            assert_eq!(co_out[i], co);
            assert_eq!(cg_out[i], cg);
        }
    }
}
