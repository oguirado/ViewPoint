use crate::isqrt::{active_mutant, isqrt_i64, MUT_BAND_OFF_BY_ONE, MUT_FLIP_DESC_BYTE_0};
use crate::par::{in_bounded_pool, split_bands};
use rayon::prelude::*;

pub const F_EDGE: u8 = 1 << 0;
pub const F_TEXTURE: u8 = 1 << 1;
pub const F_SHADE_ONLY: u8 = 1 << 2;
pub const F_SAT: u8 = 1 << 3;
pub const F_UI_OCC: u8 = 1 << 4;
pub const F_ACTOR_OCC: u8 = 1 << 5;
pub const F_GLOBAL_MV: u8 = 1 << 6;
pub const F_PAD: u8 = 1 << 7;

#[repr(C)]
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct TileDesc {
    pub morton: u16,
    pub cls: u8,
    pub flags: u8,
    pub y_q4: u16,
    pub co_h: i8,
    pub cg_h: i8,
    pub sig_q4: u16,
    pub grad_q6: u16,
    pub edge: u8,
    pub resid: u8,
    pub mv_dx: i8,
    pub mv_dy: i8,
    pub sig64: u64,
    pub xh64: u64,
}

#[inline]
fn clip_i8(val: i64) -> i8 {
    if val < -128 {
        -128
    } else if val > 127 {
        127
    } else {
        val as i8
    }
}

/// Scalar reference implementation of S1 perceptual descriptor fields for one tile.
#[allow(clippy::too_many_arguments)]
pub fn compute_tile_scalar(
    padded_rgb: &[u8],
    padded_w: usize,
    _padded_h: usize,
    luma_plane: &[i16],
    s: usize,
    tx: usize,
    ty: usize,
    morton_code: u16,
    is_pad: bool,
    tau_edge: i16,
) -> TileDesc {
    let n = (s * s) as i64;
    let tile_x0 = tx * s;
    let tile_y0 = ty * s;
    let b_size = s / 8;
    let b_pixels = (b_size * b_size) as i64;

    let mut sum_y: i64 = 0;
    let mut sum_y2: i64 = 0;
    let mut sum_co: i64 = 0;
    let mut sum_cg: i64 = 0;
    let mut sat_count: i64 = 0;
    let mut grad_sum: i64 = 0;
    let mut edge_count: i64 = 0;
    let mut sub_sums = [[0i64; 8]; 8];

    for y in 0..s {
        let gy = tile_y0 + y;
        let row_off = gy * padded_w + tile_x0;
        let luma_row = &luma_plane[row_off..row_off + s];
        let rgb_row = &padded_rgb[row_off * 3..(row_off + s) * 3];
        let sub_i = y / b_size;

        for x in 0..s {
            let y_val = luma_row[x];
            let y_i64 = y_val as i64;
            sum_y += y_i64;
            sum_y2 += y_i64 * y_i64;
            sub_sums[sub_i][x / b_size] += y_i64;

            let r = rgb_row[x * 3];
            let g = rgb_row[x * 3 + 1];
            let b = rgb_row[x * 3 + 2];

            let co = (r as i16) - (b as i16);
            let cg = (g as i16) - ((b as i16) + (co >> 1));
            sum_co += co as i64;
            sum_cg += cg as i64;

            if r.wrapping_sub(1) >= 254 || g.wrapping_sub(1) >= 254 || b.wrapping_sub(1) >= 254 {
                sat_count += 1;
            }

            let mut g_val: i16 = 0;
            if x + 1 < s {
                g_val += (luma_row[x + 1] - y_val).abs();
            }
            if y + 1 < s {
                let next_y = luma_plane[(gy + 1) * padded_w + tile_x0 + x];
                g_val += (next_y - y_val).abs();
            }
            grad_sum += g_val as i64;
            if g_val > tau_edge {
                edge_count += 1;
            }
        }
    }

    let mut sig64: u64 = 0;
    for (sub_i, row_subs) in sub_sums.iter().enumerate() {
        for (sub_j, &sub_sum) in row_subs.iter().enumerate() {
            if sub_sum * n > sum_y * b_pixels {
                sig64 |= 1u64 << (sub_i * 8 + sub_j);
            }
        }
    }

    let mut flags: u8 = 0;
    let s_i64 = s as i64;
    if 2 * edge_count >= s_i64 && edge_count < 4 * s_i64 {
        flags |= F_EDGE;
    }
    if edge_count >= 4 * s_i64 {
        flags |= F_TEXTURE;
    }
    if 4 * sat_count >= n {
        flags |= F_SAT;
    }
    if is_pad {
        flags |= F_PAD;
    }

    let y_q4 = (16 * sum_y / n) as u16;
    let co_h = clip_i8(sum_co.div_euclid(2 * n));
    let cg_h = clip_i8(sum_cg.div_euclid(2 * n));
    let sigma = isqrt_i64(n * sum_y2 - sum_y * sum_y);
    let sig_q4 = (16 * sigma / n) as u16;
    let grad_q6 = (64 * grad_sum / n) as u16;
    let edge = (255 * edge_count / n) as u8;

    TileDesc {
        morton: morton_code,
        cls: 0,
        flags,
        y_q4,
        co_h,
        cg_h,
        sig_q4,
        grad_q6,
        edge,
        resid: 0,
        mv_dx: 0,
        mv_dy: 0,
        sig64,
        xh64: 0,
    }
}

/// Write only the perceptual fields computed by `visioncore.desc._compute` into a 32-byte tile slot.
#[inline]
pub fn write_perceptual_fields(slot: &mut [u8; 32], tile: &TileDesc) {
    slot[3] = tile.flags;
    slot[4..6].copy_from_slice(&tile.y_q4.to_le_bytes());
    slot[6] = tile.co_h as u8;
    slot[7] = tile.cg_h as u8;
    slot[8..10].copy_from_slice(&tile.sig_q4.to_le_bytes());
    slot[10..12].copy_from_slice(&tile.grad_q6.to_le_bytes());
    slot[12] = tile.edge;
    slot[16..24].copy_from_slice(&tile.sig64.to_le_bytes());
    if active_mutant() == MUT_FLIP_DESC_BYTE_0 {
        // MUTANT:FLIP_DESC_BYTE_0: out_bytes[0] ^= 1;
        slot[0] ^= 1;
        slot[3] ^= 1;
    }
}

/// Compute perceptual fields for all tile ranks in `idx` and write them in-place into `out_bytes` (`N * 32` bytes).
#[allow(clippy::too_many_arguments)]
pub fn compute_tiles_into(
    out_bytes: &mut [u8],
    idx: &[i64],
    padded_rgb: &[u8],
    luma_plane: &[i16],
    s: usize,
    gw: usize,
    gh: usize,
    tx_arr: &[i32],
    ty_arr: &[i32],
    pad_flags: &[u8],
    tau_edge: i16,
) {
    let padded_w = gw * s;
    let padded_h = gh * s;
    assert_eq!(padded_rgb.len(), padded_w * padded_h * 3);
    assert_eq!(luma_plane.len(), padded_w * padded_h);
    let n_tiles = gw * gh;
    assert_eq!(out_bytes.len(), n_tiles * 32);
    if idx.is_empty() {
        return;
    }
    for &rank_i64 in idx {
        assert!(rank_i64 >= 0 && (rank_i64 as usize) < n_tiles);
    }

    if active_mutant() == MUT_BAND_OFF_BY_ONE {
        for &rank_i64 in idx {
            let base = (rank_i64 as usize) * 32;
            out_bytes[base + 4] = 0xFF;
            out_bytes[base + 5] = 0xFF;
        }
    }

    let strictly_inc = idx.windows(2).all(|w| w[0] < w[1]);

    in_bounded_pool(|n_threads| {
        let bands = split_bands(idx.len(), n_threads);
        if n_threads <= 1 || bands.len() <= 1 {
            for (start, end) in bands {
                for &rank_i64 in &idx[start..end] {
                    let rank = rank_i64 as usize;
                    let tx = tx_arr[rank] as usize;
                    let ty = ty_arr[rank] as usize;
                    let is_pad = pad_flags[rank] != 0;
                    let tile = compute_tile_scalar(
                        padded_rgb, padded_w, padded_h, luma_plane, s, tx, ty, 0, is_pad, tau_edge,
                    );
                    let base = rank * 32;
                    let slot: &mut [u8; 32] = (&mut out_bytes[base..base + 32]).try_into().unwrap();
                    write_perceptual_fields(slot, &tile);
                }
            }
        } else if strictly_inc {
            let mut tasks = Vec::with_capacity(bands.len());
            let mut out_rem = out_bytes;
            let mut prev_rank_end = 0usize;
            for (start, end) in bands {
                let idx_slice = &idx[start..end];
                let rank_lo = idx_slice[0] as usize;
                let rank_hi = idx_slice[idx_slice.len() - 1] as usize + 1;
                let skip_bytes = (rank_lo - prev_rank_end) * 32;
                let len_bytes = (rank_hi - rank_lo) * 32;
                if skip_bytes > 0 {
                    out_rem = &mut out_rem[skip_bytes..];
                }
                let (band_out, rest) = out_rem.split_at_mut(len_bytes);
                out_rem = rest;
                prev_rank_end = rank_hi;
                tasks.push((idx_slice, rank_lo, band_out));
            }
            tasks
                .into_par_iter()
                .with_max_len(1)
                .for_each(|(idx_slice, rank_lo, band_out)| {
                    for &rank_i64 in idx_slice {
                        let rank = rank_i64 as usize;
                        let tx = tx_arr[rank] as usize;
                        let ty = ty_arr[rank] as usize;
                        let is_pad = pad_flags[rank] != 0;
                        let tile = compute_tile_scalar(
                            padded_rgb, padded_w, padded_h, luma_plane, s, tx, ty, 0, is_pad,
                            tau_edge,
                        );
                        let local_base = (rank - rank_lo) * 32;
                        let slot: &mut [u8; 32] = (&mut band_out[local_base..local_base + 32])
                            .try_into()
                            .unwrap();
                        write_perceptual_fields(slot, &tile);
                    }
                });
        } else {
            let computed: Vec<Vec<(usize, TileDesc)>> = bands
                .into_par_iter()
                .with_max_len(1)
                .map(|(start, end)| {
                    let mut v = Vec::with_capacity(end - start);
                    for &rank_i64 in &idx[start..end] {
                        let rank = rank_i64 as usize;
                        let tx = tx_arr[rank] as usize;
                        let ty = ty_arr[rank] as usize;
                        let is_pad = pad_flags[rank] != 0;
                        let tile = compute_tile_scalar(
                            padded_rgb, padded_w, padded_h, luma_plane, s, tx, ty, 0, is_pad,
                            tau_edge,
                        );
                        v.push((rank, tile));
                    }
                    v
                })
                .collect();
            for band_res in computed {
                for (rank, tile) in band_res {
                    let base = rank * 32;
                    let slot: &mut [u8; 32] = (&mut out_bytes[base..base + 32]).try_into().unwrap();
                    write_perceptual_fields(slot, &tile);
                }
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::s0::luma_of_padded;

    #[test]
    fn test_compute_tile_basic() {
        let s = 16usize;
        let rgb = vec![128u8; s * s * 3];
        let luma = luma_of_padded(&rgb, s, s);
        let t = compute_tile_scalar(&rgb, s, s, &luma, s, 0, 0, 7, false, 48);
        assert_eq!(t.morton, 7);
        assert_eq!(t.y_q4, 128 * 16);
        assert_eq!(t.co_h, 0);
        assert_eq!(t.cg_h, 0);
        assert_eq!(t.sig_q4, 0);
        assert_eq!(t.grad_q6, 0);
        assert_eq!(t.edge, 0);
        assert_eq!(t.sig64, 0);
    }
}
