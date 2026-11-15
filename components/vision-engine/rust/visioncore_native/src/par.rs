#![allow(
    clippy::too_many_arguments,
    clippy::needless_range_loop,
    clippy::type_complexity
)]
use crate::isqrt::{
    active_mutant, MUT_BAND_OFF_BY_ONE, MUT_DROP_LAST_ISLAND, MUT_MERGE_DISTINCT_MOV_GROUPS,
    MUT_SORT_REGIONS_BY_BBOX, MUT_SWAP_DX_DY,
};
use rayon::prelude::*;
use rayon::{ThreadPool, ThreadPoolBuilder};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

pub const MOTION_D_MAX: i64 = 64;
pub const ISLAND_MIN_TILES: usize = 4;
pub const DEFAULT_MAX_THREADS: usize = 4;
pub const MAX_POOL_THREADS: usize = 8;

static THREADS_OVERRIDE: AtomicUsize = AtomicUsize::new(0);
static BOUNDED_POOL: Mutex<Option<(usize, Arc<ThreadPool>)>> = Mutex::new(None);

/// Default bounded thread count: `min(4, available_parallelism)` in `1..=4`.
#[inline]
pub fn default_threads() -> usize {
    std::thread::available_parallelism()
        .map(|n| n.get().min(DEFAULT_MAX_THREADS))
        .unwrap_or(1)
        .max(1)
}

/// Parse a `VISIONCORE_THREADS` value in `1..=8`, falling back to `default_threads()`.
#[inline]
pub fn parse_threads_env(val: Option<&str>) -> usize {
    if let Some(raw) = val {
        let s = raw.trim();
        if !s.is_empty() {
            if let Ok(n) = s.parse::<usize>() {
                if (1..=MAX_POOL_THREADS).contains(&n) {
                    return n;
                }
            }
        }
    }
    default_threads()
}

/// Override the thread count programmatically (`None` or `Some(0)` clears the override).
pub fn set_threads_override(n: Option<usize>) -> Result<(), &'static str> {
    match n {
        None | Some(0) => {
            THREADS_OVERRIDE.store(0, Ordering::Relaxed);
            Ok(())
        }
        Some(v) if (1..=MAX_POOL_THREADS).contains(&v) => {
            THREADS_OVERRIDE.store(v, Ordering::Relaxed);
            Ok(())
        }
        _ => Err("VISIONCORE_THREADS must be in 1..=8"),
    }
}

/// Effective bounded thread count (`1..=8`), checking programmatic override then `VISIONCORE_THREADS` env.
#[inline]
pub fn configured_threads() -> usize {
    let ov = THREADS_OVERRIDE.load(Ordering::Relaxed);
    if (1..=MAX_POOL_THREADS).contains(&ov) {
        return ov;
    }
    let env_val = std::env::var("VISIONCORE_THREADS").ok();
    parse_threads_env(env_val.as_deref())
}

fn get_bounded_pool(n_threads: usize) -> Option<Arc<ThreadPool>> {
    if n_threads <= 1 {
        return None;
    }
    let n = n_threads.min(MAX_POOL_THREADS);
    let mut guard = BOUNDED_POOL.lock().ok()?;
    if let Some((cached_n, ref pool)) = *guard {
        if cached_n == n {
            return Some(Arc::clone(pool));
        }
    }
    let pool = ThreadPoolBuilder::new()
        .num_threads(n)
        .thread_name(move |i| format!("vc-bounded-{n}-{i}"))
        .build()
        .ok()?;
    let arc = Arc::new(pool);
    *guard = Some((n, Arc::clone(&arc)));
    Some(arc)
}

/// Execute `op(n_threads)` inside the process-wide bounded rayon `ThreadPool` when `n_threads > 1`,
/// or serially on the calling thread when `n_threads == 1` (single-thread fallback).
/// Never initializes or uses rayon's global pool.
#[inline]
pub fn in_bounded_pool<R: Send>(op: impl FnOnce(usize) -> R + Send) -> R {
    let n = configured_threads();
    if n <= 1 {
        return op(1);
    }
    match get_bounded_pool(n) {
        Some(pool) => pool.install(|| op(n)),
        None => op(1),
    }
}

/// Return `(configured_threads, active_pool_threads, worker_thread_name)` for inspection/testing.
pub fn pool_status() -> (usize, usize, String) {
    let n = configured_threads();
    if n <= 1 {
        return (1, 1, "serial".to_string());
    }
    match get_bounded_pool(n) {
        Some(pool) => {
            let (nt, tname) = pool.install(|| {
                let cur = rayon::current_num_threads();
                let names: Vec<String> = (0..n)
                    .into_par_iter()
                    .map(|_| {
                        std::thread::current()
                            .name()
                            .unwrap_or("unnamed")
                            .to_string()
                    })
                    .collect();
                let worker_name = names
                    .into_iter()
                    .find(|s| s.starts_with("vc-bounded-"))
                    .unwrap_or_else(|| "vc-bounded".to_string());
                (cur, worker_name)
            });
            (n, nt, tname)
        }
        None => (n, 1, "fallback-serial".to_string()),
    }
}

/// Partition `0..total` into contiguous `(start, end)` bands for data-parallel execution.
/// Under `MUT_BAND_OFF_BY_ONE`, each band's upper boundary is shifted by `-1`.
pub fn split_bands(total: usize, n_bands: usize) -> Vec<(usize, usize)> {
    if total == 0 {
        return Vec::new();
    }
    let off_by_one = active_mutant() == MUT_BAND_OFF_BY_ONE;
    let bands = if off_by_one {
        n_bands.max(2).min(total)
    } else {
        n_bands.max(1).min(total)
    };
    let mut out = Vec::with_capacity(bands);
    for b in 0..bands {
        let start = (b * total) / bands;
        let mut end = ((b + 1) * total) / bands;
        if off_by_one && end > start {
            // MUTANT:BAND_OFF_BY_ONE: end -= 1;
            end -= 1;
        }
        if start < end {
            out.push((start, end));
        }
    }
    out
}

/// Best integer shift `d` (`cur[x] ~ prev[x - d]`) of two 1-D projections; `0` if no clear winner.
/// Matches `visioncore.change._axis_shift(cur, prev, dmax)`.
pub fn axis_shift(cur: &[i64], prev: &[i64], dmax: i64) -> i64 {
    let n = cur.len() as i64;
    assert_eq!(cur.len(), prev.len());
    if n <= 0 {
        return 0;
    }

    let mut best: Option<(i128, i64, i64)> = None;
    let mut cost0: Option<i128> = None;

    for d in -dmax..=dmax {
        if d.abs() >= n {
            continue;
        }
        let sad: i128 = if d >= 0 {
            let d_usize = d as usize;
            let len = (n - d) as usize;
            let c_slice = &cur[d_usize..d_usize + len];
            let p_slice = &prev[0..len];
            let mut s: i128 = 0;
            for i in 0..len {
                s += (c_slice[i] as i128 - p_slice[i] as i128).abs();
            }
            s
        } else {
            let neg_d_usize = (-d) as usize;
            let len = (n + d) as usize;
            let c_slice = &cur[0..len];
            let p_slice = &prev[neg_d_usize..neg_d_usize + len];
            let mut s: i128 = 0;
            for i in 0..len {
                s += (c_slice[i] as i128 - p_slice[i] as i128).abs();
            }
            s
        };

        let denom = (n - d.abs()) as i128;
        let cost = (sad * (n as i128)).div_euclid(denom);

        if d == 0 {
            cost0 = Some(cost);
        }

        let key = (cost, d.abs(), d);
        if best.is_none() || key < best.unwrap() {
            best = Some(key);
        }
    }

    match (best, cost0) {
        (Some(b), Some(c0)) => {
            if b.2 == 0 || !(3 * b.0 < c0) {
                0
            } else {
                b.2
            }
        }
        _ => 0,
    }
}

#[cfg(target_arch = "aarch64")]
#[inline]
unsafe fn project_sub_i16_neon(
    plane: &[i16],
    stride: usize,
    y0: usize,
    y1: usize,
    x0: usize,
    x1: usize,
    col_sums: &mut [i64],
    row_sums: &mut [i64],
) {
    use core::arch::aarch64::*;
    let w_sub = x1 - x0;
    for (ry, y) in (y0..y1).enumerate() {
        let row = &plane[y * stride + x0..y * stride + x1];
        let mut r_acc = vdupq_n_s64(0);
        let mut x = 0usize;
        while x + 8 <= w_sub {
            let v = vld1q_s16(row.as_ptr().add(x));
            let lo32 = vmovl_s16(vget_low_s16(v));
            let hi32 = vmovl_high_s16(v);
            let s32 = vaddq_s32(lo32, hi32);
            r_acc = vpadalq_s32(r_acc, s32);

            let c0 = vmovl_s32(vget_low_s32(lo32));
            let c1 = vmovl_high_s32(lo32);
            let c2 = vmovl_s32(vget_low_s32(hi32));
            let c3 = vmovl_high_s32(hi32);

            let p0 = col_sums.as_mut_ptr().add(x);
            vst1q_s64(p0, vaddq_s64(vld1q_s64(p0), c0));
            let p1 = col_sums.as_mut_ptr().add(x + 2);
            vst1q_s64(p1, vaddq_s64(vld1q_s64(p1), c1));
            let p2 = col_sums.as_mut_ptr().add(x + 4);
            vst1q_s64(p2, vaddq_s64(vld1q_s64(p2), c2));
            let p3 = col_sums.as_mut_ptr().add(x + 6);
            vst1q_s64(p3, vaddq_s64(vld1q_s64(p3), c3));

            x += 8;
        }
        let mut r_sum = vaddvq_s64(r_acc);
        while x < w_sub {
            let val = row[x] as i64;
            col_sums[x] += val;
            r_sum += val;
            x += 1;
        }
        row_sums[ry] = r_sum;
    }
}

/// Compute `(gdx, gdy)` from row/column projections on a sub-rectangle `[y0..y1, x0..x1]`.
pub fn global_vector_sub_i16(
    y_cur: &[i16],
    y_prev: &[i16],
    stride: usize,
    y0: usize,
    y1: usize,
    x0: usize,
    x1: usize,
    s: i64,
) -> (i64, i64) {
    let h_sub = y1.saturating_sub(y0);
    let w_sub = x1.saturating_sub(x0);
    if h_sub == 0 || w_sub == 0 {
        return (0, 0);
    }
    let dmax = s.min(MOTION_D_MAX);
    let mut cur_col = vec![0i64; w_sub];
    let mut prev_col = vec![0i64; w_sub];
    let mut cur_row = vec![0i64; h_sub];
    let mut prev_row = vec![0i64; h_sub];

    #[cfg(target_arch = "aarch64")]
    unsafe {
        project_sub_i16_neon(y_cur, stride, y0, y1, x0, x1, &mut cur_col, &mut cur_row);
        project_sub_i16_neon(y_prev, stride, y0, y1, x0, x1, &mut prev_col, &mut prev_row);
    }

    #[cfg(not(target_arch = "aarch64"))]
    {
        for (ry, y) in (y0..y1).enumerate() {
            let c_row = &y_cur[y * stride + x0..y * stride + x1];
            let p_row = &y_prev[y * stride + x0..y * stride + x1];
            let mut rc = 0i64;
            let mut rp = 0i64;
            for x in 0..w_sub {
                let cv = c_row[x] as i64;
                let pv = p_row[x] as i64;
                cur_col[x] += cv;
                prev_col[x] += pv;
                rc += cv;
                rp += pv;
            }
            cur_row[ry] = rc;
            prev_row[ry] = rp;
        }
    }

    let gdx = axis_shift(&cur_col, &prev_col, dmax);
    let gdy = axis_shift(&cur_row, &prev_row, dmax);
    if active_mutant() == MUT_SWAP_DX_DY {
        // MUTANT:SWAP_DX_DY: return (gdy, gdx);
        return (gdy, gdx);
    }
    (gdx, gdy)
}

/// Full-frame `global_vector(y_cur, y_prev, s)` matching `visioncore.change.global_vector`.
pub fn global_vector(y_cur: &[i16], y_prev: &[i16], h: usize, w: usize, s: i64) -> (i64, i64) {
    assert_eq!(y_cur.len(), h * w);
    assert_eq!(y_prev.len(), h * w);
    global_vector_sub_i16(y_cur, y_prev, w, 0, h, 0, w, s)
}

/// `global_vector` for `i64` planes.
pub fn global_vector_i64(y_cur: &[i64], y_prev: &[i64], h: usize, w: usize, s: i64) -> (i64, i64) {
    assert_eq!(y_cur.len(), h * w);
    assert_eq!(y_prev.len(), h * w);
    if h == 0 || w == 0 {
        return (0, 0);
    }
    let dmax = s.min(MOTION_D_MAX);
    let mut cur_col = vec![0i64; w];
    let mut prev_col = vec![0i64; w];
    let mut cur_row = vec![0i64; h];
    let mut prev_row = vec![0i64; h];
    for y in 0..h {
        let c_row = &y_cur[y * w..(y + 1) * w];
        let p_row = &y_prev[y * w..(y + 1) * w];
        let mut rc = 0i64;
        let mut rp = 0i64;
        for x in 0..w {
            let cv = c_row[x];
            let pv = p_row[x];
            cur_col[x] = cur_col[x].wrapping_add(cv);
            prev_col[x] = prev_col[x].wrapping_add(pv);
            rc = rc.wrapping_add(cv);
            rp = rp.wrapping_add(pv);
        }
        cur_row[y] = rc;
        prev_row[y] = rp;
    }
    let gdx = axis_shift(&cur_col, &prev_col, dmax);
    let gdy = axis_shift(&cur_row, &prev_row, dmax);
    if active_mutant() == MUT_SWAP_DX_DY {
        return (gdy, gdx);
    }
    (gdx, gdy)
}

fn tile_sums_generic_into<T: Copy + Sync>(
    absdiff: &[T],
    s: usize,
    gh: usize,
    gw: usize,
    out: &mut [i64],
    to_i64: fn(T) -> i64,
) {
    let w = gw * s;
    assert_eq!(absdiff.len(), gh * s * w);
    assert_eq!(out.len(), gh * gw);
    if active_mutant() == MUT_BAND_OFF_BY_ONE {
        out.fill(-1);
    } else {
        out.fill(0);
    }
    if gh == 0 || gw == 0 || s == 0 {
        return;
    }
    in_bounded_pool(|n_threads| {
        if gh == 1 {
            let bands = split_bands(gw, n_threads);
            for (tx0, tx1) in bands {
                out[tx0..tx1].fill(0);
                for sy in 0..s {
                    let row = &absdiff[sy * w..(sy + 1) * w];
                    for tx in tx0..tx1 {
                        let seg = &row[tx * s..(tx + 1) * s];
                        let sum: i64 = seg.iter().copied().map(to_i64).sum();
                        out[tx] += sum;
                    }
                }
            }
            return;
        }
        let bands = split_bands(gh, n_threads);
        if n_threads <= 1 || bands.len() <= 1 {
            for (ty0, ty1) in bands {
                for ty in ty0..ty1 {
                    let out_row = &mut out[ty * gw..(ty + 1) * gw];
                    out_row.fill(0);
                    for sy in 0..s {
                        let row = &absdiff[(ty * s + sy) * w..(ty * s + sy + 1) * w];
                        for tx in 0..gw {
                            let seg = &row[tx * s..(tx + 1) * s];
                            let sum: i64 = seg.iter().copied().map(to_i64).sum();
                            out_row[tx] += sum;
                        }
                    }
                }
            }
        } else {
            let mut tasks: Vec<(usize, usize, &mut [i64])> = Vec::with_capacity(bands.len());
            let mut rem = out;
            let mut prev_end = 0usize;
            for (ty0, ty1) in bands {
                let skip = (ty0 - prev_end) * gw;
                let len = (ty1 - ty0) * gw;
                if skip > 0 {
                    rem = &mut rem[skip..];
                }
                let (band_out, rest) = rem.split_at_mut(len);
                rem = rest;
                prev_end = ty1;
                tasks.push((ty0, ty1, band_out));
            }
            tasks
                .into_par_iter()
                .with_max_len(1)
                .for_each(|(ty0, ty1, band_out)| {
                    band_out.fill(0);
                    for (local_ty, ty) in (ty0..ty1).enumerate() {
                        let out_row = &mut band_out[local_ty * gw..(local_ty + 1) * gw];
                        for sy in 0..s {
                            let row = &absdiff[(ty * s + sy) * w..(ty * s + sy + 1) * w];
                            for tx in 0..gw {
                                let seg = &row[tx * s..(tx + 1) * s];
                                let sum: i64 = seg.iter().copied().map(to_i64).sum();
                                out_row[tx] += sum;
                            }
                        }
                    }
                });
        }
    });
}

/// `_tile_sums(absdiff, s, gh, gw)` for `i16`, `i32`, or `i64` inputs into `(gh, gw)` `i64` output.
pub fn tile_sums_i16_into(absdiff: &[i16], s: usize, gh: usize, gw: usize, out: &mut [i64]) {
    tile_sums_generic_into(absdiff, s, gh, gw, out, |v| v as i64);
}

pub fn tile_sums_i32_into(absdiff: &[i32], s: usize, gh: usize, gw: usize, out: &mut [i64]) {
    tile_sums_generic_into(absdiff, s, gh, gw, out, |v| v as i64);
}

pub fn tile_sums_i64_into(absdiff: &[i64], s: usize, gh: usize, gw: usize, out: &mut [i64]) {
    tile_sums_generic_into(absdiff, s, gh, gw, out, |v| v);
}

/// 8-connected labelling of a bool `(gh, gw)` mask (`visioncore.change._islands`).
/// Labels `1..=n` in raster order of first tile, `0` = background.
pub fn s2_islands_into(mask: &[u8], gh: usize, gw: usize, lab: &mut [i32]) -> i32 {
    assert_eq!(mask.len(), gh * gw);
    assert_eq!(lab.len(), gh * gw);
    lab.fill(0);
    let mut n: i32 = 0;
    let mut q: Vec<(usize, usize)> = Vec::new();

    for y in 0..gh {
        for x in 0..gw {
            let idx = y * gw + x;
            if mask[idx] != 0 && lab[idx] == 0 {
                n += 1;
                lab[idx] = n;
                q.clear();
                q.push((y, x));
                while let Some((cy, cx)) = q.pop() {
                    let y_lo = cy.saturating_sub(1);
                    let y_hi = (cy + 2).min(gh);
                    let x_lo = cx.saturating_sub(1);
                    let x_hi = (cx + 2).min(gw);
                    for ny in y_lo..y_hi {
                        for nx in x_lo..x_hi {
                            let nidx = ny * gw + nx;
                            if mask[nidx] != 0 && lab[nidx] == 0 {
                                lab[nidx] = n;
                                q.push((ny, nx));
                            }
                        }
                    }
                }
            }
        }
    }

    if active_mutant() == MUT_DROP_LAST_ISLAND && n > 0 {
        // MUTANT:DROP_LAST_ISLAND: drop highest island label
        for v in lab.iter_mut() {
            if *v == n {
                *v = 0;
            }
        }
        return n - 1;
    }

    n
}

/// `visioncore.change.island_vector(lab_mask, y_cur, y_prev, s)` for `i16` luma planes.
#[allow(clippy::too_many_arguments)]
pub fn island_vector_i16(
    lab_mask: &[u8],
    gh: usize,
    gw: usize,
    y_cur: &[i16],
    y_prev: &[i16],
    h: usize,
    w: usize,
    s: usize,
) -> Result<(i64, i64), &'static str> {
    assert_eq!(lab_mask.len(), gh * gw);
    assert_eq!(y_cur.len(), h * w);
    assert_eq!(y_prev.len(), h * w);

    let mut count = 0usize;
    let mut ty_min = gh;
    let mut ty_max = 0usize;
    let mut tx_min = gw;
    let mut tx_max = 0usize;

    for ty in 0..gh {
        for tx in 0..gw {
            if lab_mask[ty * gw + tx] != 0 {
                count += 1;
                if ty < ty_min {
                    ty_min = ty;
                }
                if ty > ty_max {
                    ty_max = ty;
                }
                if tx < tx_min {
                    tx_min = tx;
                }
                if tx > tx_max {
                    tx_max = tx;
                }
            }
        }
    }

    if count == 0 {
        return Err("zero-size array to reduction operation minimum which has no identity");
    }

    let y0 = ty_min.saturating_sub(1) * s;
    let y1 = ((ty_max + 2) * s).min(h);
    let x0 = tx_min.saturating_sub(1) * s;
    let x1 = ((tx_max + 2) * s).min(w);

    Ok(global_vector_sub_i16(
        y_cur, y_prev, w, y0, y1, x0, x1, s as i64,
    ))
}

/// `visioncore.change.island_vector(lab_mask, y_cur, y_prev, s)` for `i64` luma planes.
#[allow(clippy::too_many_arguments)]
pub fn island_vector_i64(
    lab_mask: &[u8],
    gh: usize,
    gw: usize,
    y_cur: &[i64],
    y_prev: &[i64],
    h: usize,
    w: usize,
    s: usize,
) -> Result<(i64, i64), &'static str> {
    let mut count = 0usize;
    let mut ty_min = gh;
    let mut ty_max = 0usize;
    let mut tx_min = gw;
    let mut tx_max = 0usize;

    for ty in 0..gh {
        for tx in 0..gw {
            if lab_mask[ty * gw + tx] != 0 {
                count += 1;
                ty_min = ty_min.min(ty);
                ty_max = ty_max.max(ty);
                tx_min = tx_min.min(tx);
                tx_max = tx_max.max(tx);
            }
        }
    }

    if count == 0 {
        return Err("zero-size array to reduction operation minimum which has no identity");
    }

    let y0 = ty_min.saturating_sub(1) * s;
    let y1 = ((ty_max + 2) * s).min(h);
    let x0 = tx_min.saturating_sub(1) * s;
    let x1 = ((tx_max + 2) * s).min(w);
    let h_sub = y1.saturating_sub(y0);
    let w_sub = x1.saturating_sub(x0);
    if h_sub == 0 || w_sub == 0 {
        return Ok((0, 0));
    }
    let dmax = (s as i64).min(MOTION_D_MAX);
    let mut cur_col = vec![0i64; w_sub];
    let mut prev_col = vec![0i64; w_sub];
    let mut cur_row = vec![0i64; h_sub];
    let mut prev_row = vec![0i64; h_sub];
    for (ry, y) in (y0..y1).enumerate() {
        let c_row = &y_cur[y * w + x0..y * w + x1];
        let p_row = &y_prev[y * w + x0..y * w + x1];
        let mut rc = 0i64;
        let mut rp = 0i64;
        for x in 0..w_sub {
            cur_col[x] += c_row[x];
            prev_col[x] += p_row[x];
            rc += c_row[x];
            rp += p_row[x];
        }
        cur_row[ry] = rc;
        prev_row[ry] = rp;
    }
    let gdx = axis_shift(&cur_col, &prev_col, dmax);
    let gdy = axis_shift(&cur_row, &prev_row, dmax);
    let mv = if active_mutant() == MUT_SWAP_DX_DY {
        (gdy, gdx)
    } else {
        (gdx, gdy)
    };
    Ok(mv)
}

// ============================================================================
// Brief 4: S4 region islands, regions_of, adjacency, and match_regions
// ============================================================================

#[inline]
pub fn compact_u16(mut v: u16) -> u32 {
    v &= 0x5555;
    v = (v | (v >> 1)) & 0x3333;
    v = (v | (v >> 2)) & 0x0F0F;
    ((v | (v >> 4)) & 0x00FF) as u32
}

#[inline]
pub fn unmorton(m: u16) -> (u32, u32) {
    (compact_u16(m), compact_u16(m >> 1))
}

#[inline]
pub fn spread_u32(mut v: u32) -> u32 {
    v &= 0xFF;
    v = (v | (v << 4)) & 0x0F0F;
    v = (v | (v << 2)) & 0x3333;
    v = (v | (v << 1)) & 0x5555;
    v
}

#[inline]
pub fn morton(tx: u32, ty: u32) -> u16 {
    (spread_u32(tx) | (spread_u32(ty) << 1)) as u16
}

#[inline]
pub fn aligned_block(tx0: u32, ty0: u32, tx1: u32, ty1: u32) -> (u32, u16, u16) {
    let mut k = 0u32;
    while (tx0 >> k) != (tx1 >> k) || (ty0 >> k) != (ty1 >> k) {
        k += 1;
    }
    let m0 = morton((tx0 >> k) << k, (ty0 >> k) << k);
    let span = 1u32 << (2 * k);
    let m1 = (m0 as u32 + span - 1) as u16;
    (k, m0, m1)
}

#[inline]
fn kind_of(flags: u8, edge: u8, s: u32) -> usize {
    if (flags & (1 << 1)) != 0 {
        1 // TEXTURE
    } else if (flags & (1 << 0)) != 0 {
        0 // EDGE
    } else if (edge as u32) * s * 8 < 255 {
        2 // SMOOTH
    } else {
        3 // MIXED
    }
}

/// Union-find 8-connected components on `cells: &[(tx, ty, morton)]` (in dict insertion order).
/// Matches `visioncore.regions._islands(cells)` bit-for-bit.
pub fn s4_islands(cells: &[(i32, i32, u16)]) -> Vec<Vec<(i32, i32)>> {
    let n = cells.len();
    if n == 0 {
        return Vec::new();
    }

    let mut pos_map: HashMap<(i32, i32), usize> = HashMap::with_capacity(n * 2);
    for (i, &(tx, ty, _)) in cells.iter().enumerate() {
        pos_map.insert((tx, ty), i);
    }

    let mut parent: Vec<usize> = (0..n).collect();
    fn find(parent: &mut [usize], mut i: usize) -> usize {
        while parent[i] != i {
            let p = parent[i];
            parent[i] = parent[p];
            i = parent[i];
        }
        i
    }

    // Raster order `(ty, tx)` matching `sorted(cells, key=lambda k: (k[1], k[0]))`
    let mut raster_order: Vec<usize> = (0..n).collect();
    raster_order.sort_by_key(|&i| (cells[i].1, cells[i].0));

    const NB8: [(i32, i32); 8] = [
        (-1, -1),
        (0, -1),
        (1, -1),
        (-1, 0),
        (1, 0),
        (-1, 1),
        (0, 1),
        (1, 1),
    ];

    for &i in &raster_order {
        let (tx, ty, _) = cells[i];
        for &(dx, dy) in &NB8 {
            if let Some(&j) = pos_map.get(&(tx + dx, ty + dy)) {
                let a = find(&mut parent, i);
                let b = find(&mut parent, j);
                if a != b {
                    if cells[a].2 <= cells[b].2 {
                        parent[b] = a;
                    } else {
                        parent[a] = b;
                    }
                }
            }
        }
    }

    // Preserve insertion order of `cells` when grouping by root
    let mut root_to_comp: HashMap<usize, usize> = HashMap::new();
    let mut comps: Vec<Vec<(i32, i32)>> = Vec::new();
    for i in 0..n {
        let r = find(&mut parent, i);
        let c_idx = match root_to_comp.get(&r) {
            Some(&idx) => idx,
            None => {
                let idx = comps.len();
                root_to_comp.insert(r, idx);
                comps.push(Vec::new());
                idx
            }
        };
        comps[c_idx].push((cells[i].0, cells[i].1));
    }

    if active_mutant() == MUT_DROP_LAST_ISLAND && !comps.is_empty() {
        comps.pop();
    }

    comps
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RawRegion {
    pub min_morton: u16,
    pub group_code: u8, // 0 = CHG, 1 = MOV, 2 = FRZ
    pub cls_idx: usize,
    pub cls_hist: [usize; 7],
    pub n_tiles: usize,
    pub bbox_t: [i32; 4],
    pub bbox_px: [i32; 4],
    pub mv: [i32; 2],
    pub r_max: u32,
    pub r_sum: u64,
    pub edge_ppm: u64,
    pub kind_idx: usize,
    pub shade_only: bool,
    pub occ_ui: bool,
    pub occ_actor: bool,
    pub block_k: u32,
    pub block_b0: u16,
    pub block_b1: u16,
    pub spans: Vec<(u16, u16)>,
}

/// Compute all S4 regions from raw 32-byte `TILE_DTYPE` records.
/// Returns `Err(bad_morton)` if any tile's Morton coordinate is outside `(gw, gh)`.
pub fn regions_of_raw(
    tile_bytes: &[u8],
    w: u32,
    h: u32,
    s: u32,
    gw: u32,
    gh: u32,
) -> Result<Vec<RawRegion>, u16> {
    assert_eq!(tile_bytes.len() % 32, 0);
    let n_tiles = tile_bytes.len() / 32;

    // Vectorised range check over every morton first (matching `visioncore.regions.regions_of`)
    for i in 0..n_tiles {
        let base = i * 32;
        let m = u16::from_le_bytes([tile_bytes[base], tile_bytes[base + 1]]);
        let (tx, ty) = unmorton(m);
        if tx >= gw || ty >= gh {
            return Err(m);
        }
    }

    let merge_mov = active_mutant() == MUT_MERGE_DISTINCT_MOV_GROUPS;
    let mut group_order: Vec<(u8, i32, i32)> = Vec::new();
    let mut groups: HashMap<(u8, i32, i32), Vec<(i32, i32, u16)>> = HashMap::new();
    // Tile info indexed by morton (u16): (cls, flags, resid, edge)
    let mut info: HashMap<u16, (u8, u8, u8, u8)> = HashMap::new();

    for i in 0..n_tiles {
        let base = i * 32;
        let cls = tile_bytes[base + 2];
        if !(2..=6).contains(&cls) {
            continue;
        }
        let m = u16::from_le_bytes([tile_bytes[base], tile_bytes[base + 1]]);
        let flags = tile_bytes[base + 3];
        let edge = tile_bytes[base + 12];
        let resid = tile_bytes[base + 13];
        let mv_dx = (tile_bytes[base + 14] as i8) as i32;
        let mv_dy = (tile_bytes[base + 15] as i8) as i32;

        let g_key = match cls {
            2 | 4 | 6 => (0u8, 0, 0),
            3 => {
                if merge_mov {
                    // MUTANT:MERGE_DISTINCT_MOV_GROUPS: g_key = (1u8, 0, 0);
                    (1u8, 0, 0)
                } else {
                    (1u8, mv_dx, mv_dy)
                }
            }
            5 => (2u8, 0, 0),
            _ => unreachable!(),
        };

        let (tx, ty) = unmorton(m);
        let entry = groups.entry(g_key).or_insert_with(|| {
            group_order.push(g_key);
            Vec::new()
        });
        entry.push((tx as i32, ty as i32, m));
        info.insert(m, (cls, flags, resid, edge));
    }

    let mut out: Vec<RawRegion> = Vec::new();
    for g_key in group_order {
        let cells = &groups[&g_key];
        let cell_morton: HashMap<(i32, i32), u16> =
            cells.iter().map(|&(tx, ty, m)| ((tx, ty), m)).collect();
        for isl in s4_islands(cells) {
            if isl.is_empty() {
                continue;
            }
            let mut ms: Vec<u16> = isl.iter().map(|c| cell_morton[c]).collect();
            ms.sort_unstable();
            let min_morton = ms[0];
            let n = ms.len();

            let mut hist = [0usize; 7];
            let mut kinds = [0usize; 4];
            let mut rmax = 0u32;
            let mut rsum = 0u64;
            let mut esum = 0u64;
            let mut shade = 0u64;
            let mut occ_ui = false;
            let mut occ_ac = false;

            for &m in &ms {
                let (cls, fl, res, edge) = info[&m];
                hist[cls as usize] += 1;
                kinds[kind_of(fl, edge, s)] += 1;
                if (res as u32) > rmax {
                    rmax = res as u32;
                }
                rsum += res as u64;
                esum += edge as u64;
                if (fl & (1 << 2)) != 0 {
                    shade += 1;
                }
                if (fl & (1 << 4)) != 0 {
                    occ_ui = true;
                }
                if (fl & (1 << 5)) != 0 {
                    occ_ac = true;
                }
            }

            let mut tx0 = i32::MAX;
            let mut ty0 = i32::MAX;
            let mut tx1 = i32::MIN;
            let mut ty1 = i32::MIN;
            for &(tx, ty) in &isl {
                tx0 = tx0.min(tx);
                ty0 = ty0.min(ty);
                tx1 = tx1.max(tx);
                ty1 = ty1.max(ty);
            }

            let s_i32 = s as i32;
            let x = tx0 * s_i32;
            let y = ty0 * s_i32;
            let w_px = (w as i32).min((tx1 + 1) * s_i32) - x;
            let h_px = (h as i32).min((ty1 + 1) * s_i32) - y;
            let bbox_t = [tx0, ty0, tx1, ty1];
            let bbox_px = [x, y, w_px, h_px];

            let (block_k, block_b0, block_b1) =
                aligned_block(tx0 as u32, ty0 as u32, tx1 as u32, ty1 as u32);

            let mut cls_idx = 0usize;
            let mut cls_max = 0usize;
            for (idx, &cnt) in hist.iter().enumerate() {
                if cnt > cls_max {
                    cls_max = cnt;
                    cls_idx = idx;
                }
            }

            let mut kind_idx = 0usize;
            let mut kind_max = 0usize;
            for (idx, &cnt) in kinds.iter().enumerate() {
                if cnt > kind_max {
                    kind_max = cnt;
                    kind_idx = idx;
                }
            }

            let mut spans: Vec<(u16, u16)> = Vec::new();
            for &m in &ms {
                if let Some(last) = spans.last_mut() {
                    if last.1.wrapping_add(1) == m {
                        last.1 = m;
                        continue;
                    }
                }
                spans.push((m, m));
            }

            let mv = if g_key.0 == 1 {
                [g_key.1, g_key.2]
            } else {
                [0, 0]
            };

            out.push(RawRegion {
                min_morton,
                group_code: g_key.0,
                cls_idx,
                cls_hist: hist,
                n_tiles: n,
                bbox_t,
                bbox_px,
                mv,
                r_max: rmax,
                r_sum: rsum,
                edge_ppm: (1_000_000 * esum) / (255 * (n as u64)),
                kind_idx,
                shade_only: 1_000_000 * shade >= 900_000 * (n as u64),
                occ_ui,
                occ_actor: occ_ac,
                block_k,
                block_b0,
                block_b1,
                spans,
            });
        }
    }

    if active_mutant() == MUT_SORT_REGIONS_BY_BBOX {
        // MUTANT:SORT_REGIONS_BY_BBOX: out.sort_by_key(|r| r.bbox_t);
        out.sort_by_key(|r| (r.bbox_t[1], r.bbox_t[0], r.bbox_t[3], r.bbox_t[2]));
    } else {
        out.sort_by_key(|r| r.min_morton);
    }

    Ok(out)
}

/// Compute `visioncore.regions.adjacency` from `(region_id, spans)` pairs.
pub fn adjacency_from_spans(regions: &[(String, Vec<(u16, u16)>)]) -> Vec<(String, String, usize)> {
    if regions.is_empty() {
        return Vec::new();
    }
    let mut owner = vec![u32::MAX; 256 * 256];
    let mut occupied: Vec<(u32, u32)> = Vec::new();

    for (r_idx, (_id, spans)) in regions.iter().enumerate() {
        for &(a, b) in spans {
            for m in a..=b {
                let (tx, ty) = unmorton(m);
                let pos = (ty as usize) * 256 + (tx as usize);
                if owner[pos] == u32::MAX {
                    occupied.push((tx, ty));
                }
                owner[pos] = r_idx as u32;
            }
        }
    }

    let mut cnt: BTreeMap<(String, String), usize> = BTreeMap::new();
    const HALF_NB: [(i32, i32); 4] = [(1, -1), (1, 0), (1, 1), (0, 1)];

    for (tx, ty) in occupied {
        let a_idx = owner[(ty as usize) * 256 + (tx as usize)] as usize;
        let a_id = &regions[a_idx].0;
        for &(dx, dy) in &HALF_NB {
            let nx = tx as i32 + dx;
            let ny = ty as i32 + dy;
            if (0..256).contains(&nx) && (0..256).contains(&ny) {
                let b_raw = owner[(ny as usize) * 256 + (nx as usize)];
                if b_raw != u32::MAX && (b_raw as usize) != a_idx {
                    let b_id = &regions[b_raw as usize].0;
                    let key = if a_id < b_id {
                        (a_id.clone(), b_id.clone())
                    } else {
                        (b_id.clone(), a_id.clone())
                    };
                    *cnt.entry(key).or_insert(0) += 1;
                }
            }
        }
    }

    cnt.into_iter().map(|((a, b), c)| (a, b, c)).collect()
}

#[derive(Debug, Clone)]
pub struct MatchRegionInput {
    pub id: String,
    pub group: String,
    pub mv: [i64; 2],
    pub n_tiles: usize,
    pub spans: Vec<(u16, u16)>,
}

pub type MatchLinksResult = (
    Vec<(String, String, usize)>,
    Vec<(String, String)>,
    Vec<(String, String)>,
);

/// Compute `visioncore.regions.match_regions` correspondence from extracted region summaries.
pub fn match_regions_from_spans(
    prev: &[MatchRegionInput],
    cur: &[MatchRegionInput],
    global_mv: (i64, i64),
    s: i64,
) -> MatchLinksResult {
    let gsx = (global_mv.0 + s / 2).div_euclid(s);
    let gsy = (global_mv.1 + s / 2).div_euclid(s);

    let mut owner = vec![u32::MAX; 65536];
    for (c_idx, r) in cur.iter().enumerate() {
        for &(a, b) in &r.spans {
            for m in a..=b {
                owner[m as usize] = c_idx as u32;
            }
        }
    }

    let mut edges: HashMap<(usize, usize), usize> = HashMap::new();
    for (p_idx, a) in prev.iter().enumerate() {
        let (shx, shy) = if a.group == "MOV" {
            (
                (a.mv[0] + s / 2).div_euclid(s),
                (a.mv[1] + s / 2).div_euclid(s),
            )
        } else {
            (gsx, gsy)
        };
        for &(m0, m1) in &a.spans {
            for m in m0..=m1 {
                let (tx, ty) = unmorton(m);
                let ntx = tx as i64 + shx;
                let nty = ty as i64 + shy;
                if !(0..=255).contains(&ntx) || !(0..=255).contains(&nty) {
                    continue;
                }
                let nm = morton(ntx as u32, nty as u32) as usize;
                let b_raw = owner[nm];
                if b_raw != u32::MAX {
                    *edges.entry((p_idx, b_raw as usize)).or_insert(0) += 1;
                }
            }
        }
    }

    let mut kept: Vec<(isize, String, String)> = Vec::new();
    for ((p_idx, c_idx), ov) in edges {
        let min_sz = prev[p_idx].n_tiles.min(cur[c_idx].n_tiles);
        if 1_000_000 * (ov as u64) >= 500_000 * (min_sz as u64) {
            kept.push((
                -(ov as isize),
                prev[p_idx].id.clone(),
                cur[c_idx].id.clone(),
            ));
        }
    }
    kept.sort();

    let mut ma: HashSet<String> = HashSet::new();
    let mut mb: HashSet<String> = HashSet::new();
    let mut links: Vec<(String, String, usize)> = Vec::new();
    let mut rest: Vec<(String, String)> = Vec::new();

    for (neg_ov, a, b) in kept {
        let ov = (-neg_ov) as usize;
        if ma.contains(&a) || mb.contains(&b) {
            rest.push((a, b));
        } else {
            ma.insert(a.clone());
            mb.insert(b.clone());
            links.push((a, b, ov));
        }
    }
    links.sort();

    let mut split: Vec<(String, String)> = rest
        .iter()
        .filter(|(a, _)| ma.contains(a))
        .cloned()
        .collect();
    split.sort();

    let mut merge: Vec<(String, String)> = rest
        .iter()
        .filter(|(a, _)| !ma.contains(a))
        .cloned()
        .collect();
    merge.sort();

    (links, split, merge)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::isqrt::{set_active_mutant, MUT_NONE, TEST_MUTEX};
    use crate::s0::{
        luma_of_padded_into, tile_sad_windows_i64_into, tile_sad_windows_into,
        tile_sad_windows_scalar, ycocg_into, SadWinError,
    };
    use crate::s1::compute_tiles_into;

    #[test]
    fn test_axis_shift_basic() {
        let _lock = TEST_MUTEX.lock().unwrap_or_else(|e| e.into_inner());
        set_active_mutant(MUT_NONE);
        let prev: Vec<i64> = (0..32).map(|x| (x * 17) % 100).collect();
        let mut cur = vec![0i64; 32];
        for x in 3..32 {
            cur[x] = prev[x - 3];
        }
        assert_eq!(axis_shift(&cur, &prev, 8), 3);
    }

    #[test]
    fn test_s2_islands() {
        let _lock = TEST_MUTEX.lock().unwrap_or_else(|e| e.into_inner());
        set_active_mutant(MUT_NONE);
        let mask = [1u8, 0, 1, 1, 0, 0, 0, 0, 1];
        let mut lab = [0i32; 9];
        let n = s2_islands_into(&mask, 3, 3, &mut lab);
        assert_eq!(n, 3);
        assert_eq!(lab[0], 1);
        assert_eq!(lab[2], 2);
        assert_eq!(lab[8], 3);
    }

    #[test]
    fn test_pool_config_and_bounded_workers() {
        let _lock = TEST_MUTEX.lock().unwrap_or_else(|e| e.into_inner());
        set_active_mutant(MUT_NONE);
        let def = default_threads();
        assert!((1..=DEFAULT_MAX_THREADS).contains(&def));
        assert_eq!(parse_threads_env(None), def);
        assert_eq!(parse_threads_env(Some("")), def);
        assert_eq!(parse_threads_env(Some("0")), def);
        assert_eq!(parse_threads_env(Some("9")), def);
        assert_eq!(parse_threads_env(Some("invalid")), def);
        for n in 1..=8 {
            assert_eq!(parse_threads_env(Some(&n.to_string())), n);
        }

        // Single-thread fallback
        set_threads_override(Some(1)).unwrap();
        let (cfg1, act1, name1) = pool_status();
        assert_eq!((cfg1, act1), (1, 1));
        assert_eq!(name1, "serial");

        // Bounded pool for 2..=4
        for n in 2..=4 {
            set_threads_override(Some(n)).unwrap();
            let (cfgn, actn, namen) = pool_status();
            assert_eq!(cfgn, n);
            assert_eq!(actn, n);
            assert!(
                namen.starts_with(&format!("vc-bounded-{n}-")),
                "unexpected worker name: {namen}"
            );
        }
        assert!(set_threads_override(Some(9)).is_err());
        set_threads_override(None).unwrap();
    }

    #[test]
    fn test_pooled_kernels_parity_and_off_by_one_band_mutant() {
        let _lock = TEST_MUTEX.lock().unwrap_or_else(|e| e.into_inner());
        set_active_mutant(MUT_NONE);

        let s = 16usize;
        let gw = 12usize;
        let gh = 8usize;
        let w = gw * s;
        let h = gh * s;
        let n_tiles = gw * gh;
        let n_px = w * h;

        let mut rgb = vec![0u8; n_px * 3];
        for (i, v) in rgb.iter_mut().enumerate() {
            *v = ((i * 73 + 19) & 0xFF) as u8;
        }
        let mut y_prev = vec![0i16; n_px];
        for (i, v) in y_prev.iter_mut().enumerate() {
            *v = (((i * 41 + 7) % 511) as i16) - 255;
        }

        let mut tx_arr = Vec::with_capacity(n_tiles);
        let mut ty_arr = Vec::with_capacity(n_tiles);
        let mut tx_i64 = Vec::with_capacity(n_tiles);
        let mut ty_i64 = Vec::with_capacity(n_tiles);
        let mut pad_flags = Vec::with_capacity(n_tiles);
        for ty in 0..gh {
            for tx in 0..gw {
                tx_arr.push(tx as i32);
                ty_arr.push(ty as i32);
                tx_i64.push(tx as i64);
                ty_i64.push(ty as i64);
                pad_flags.push(u8::from(tx == gw - 1 || ty == gh - 1));
            }
        }
        let idx: Vec<i64> = (0..n_tiles as i64).collect();

        // Reference serial run (threads = 1)
        set_threads_override(Some(1)).unwrap();
        let mut ref_luma = vec![0i16; n_px];
        luma_of_padded_into(&rgb, &mut ref_luma);

        let mut ref_y = vec![0i16; n_px];
        let mut ref_co = vec![0i16; n_px];
        let mut ref_cg = vec![0i16; n_px];
        ycocg_into(&rgb, &mut ref_y, &mut ref_co, &mut ref_cg);

        let mut ref_tiles = vec![0u8; n_tiles * 32];
        compute_tiles_into(
            &mut ref_tiles,
            &idx,
            &rgb,
            &ref_luma,
            s,
            gw,
            gh,
            &tx_arr,
            &ty_arr,
            &pad_flags,
            48,
        );

        let mut ref_sad = vec![0i64; n_tiles];
        let mut ref_cnt = vec![0i64; n_tiles];
        tile_sad_windows_into(
            &ref_luma,
            &y_prev,
            h,
            w,
            3,
            -2,
            s,
            &tx_i64,
            &ty_i64,
            &mut ref_sad,
            &mut ref_cnt,
        )
        .unwrap();
        let (scalar_sad, scalar_cnt) =
            tile_sad_windows_scalar(&ref_luma, &y_prev, h, w, 3, -2, s, &tx_i64, &ty_i64).unwrap();
        assert_eq!(ref_sad, scalar_sad);
        assert_eq!(ref_cnt, scalar_cnt);

        let mut ref_sums = vec![0i64; n_tiles];
        tile_sums_i16_into(&ref_luma, s, gh, gw, &mut ref_sums);

        // Compare threads 1..=4 (and 8) + repeated runs against serial reference
        for nt in [1usize, 2, 3, 4, 8] {
            set_threads_override(Some(nt)).unwrap();
            for _rep in 0..2 {
                let mut luma = vec![0i16; n_px];
                luma_of_padded_into(&rgb, &mut luma);
                assert_eq!(luma, ref_luma, "luma mismatch at threads={nt}");

                let mut y = vec![0i16; n_px];
                let mut co = vec![0i16; n_px];
                let mut cg = vec![0i16; n_px];
                ycocg_into(&rgb, &mut y, &mut co, &mut cg);
                assert_eq!(y, ref_y, "ycocg.y mismatch at threads={nt}");
                assert_eq!(co, ref_co, "ycocg.co mismatch at threads={nt}");
                assert_eq!(cg, ref_cg, "ycocg.cg mismatch at threads={nt}");

                let mut tiles = vec![0u8; n_tiles * 32];
                compute_tiles_into(
                    &mut tiles, &idx, &rgb, &luma, s, gw, gh, &tx_arr, &ty_arr, &pad_flags, 48,
                );
                assert_eq!(tiles, ref_tiles, "compute_tiles mismatch at threads={nt}");

                let mut sad = vec![0i64; n_tiles];
                let mut cnt = vec![0i64; n_tiles];
                tile_sad_windows_into(
                    &luma, &y_prev, h, w, 3, -2, s, &tx_i64, &ty_i64, &mut sad, &mut cnt,
                )
                .unwrap();
                assert_eq!(sad, ref_sad, "sad mismatch at threads={nt}");
                assert_eq!(cnt, ref_cnt, "cnt mismatch at threads={nt}");

                let mut sums = vec![0i64; n_tiles];
                tile_sums_i16_into(&luma, s, gh, gw, &mut sums);
                assert_eq!(sums, ref_sums, "tile_sums mismatch at threads={nt}");
            }
        }

        // Preserved IndexOutOfBounds error under pool
        set_threads_override(Some(4)).unwrap();
        let mut bad_sad = vec![0i64; 1];
        let mut bad_cnt = vec![0i64; 1];
        assert_eq!(
            tile_sad_windows_into(
                &ref_luma,
                &y_prev,
                h,
                w,
                0,
                0,
                s,
                &[gw as i64],
                &[0],
                &mut bad_sad,
                &mut bad_cnt,
            ),
            Err(SadWinError::IndexOutOfBounds)
        );
        let luma_i64: Vec<i64> = ref_luma.iter().map(|&v| v as i64).collect();
        let prev_i64: Vec<i64> = y_prev.iter().map(|&v| v as i64).collect();
        assert_eq!(
            tile_sad_windows_i64_into(
                &luma_i64,
                &prev_i64,
                h,
                w,
                0,
                0,
                s,
                &[-(gw as i64) - 1],
                &[0],
                &mut bad_sad,
                &mut bad_cnt,
            ),
            Err(SadWinError::IndexOutOfBounds)
        );

        // Named mutation: split tiles with an off-by-one band boundary -> parity fails
        set_active_mutant(MUT_BAND_OFF_BY_ONE);
        let mut mut_luma = vec![0i16; n_px];
        luma_of_padded_into(&rgb, &mut mut_luma);
        assert_ne!(mut_luma, ref_luma);

        let mut mut_y = vec![0i16; n_px];
        let mut mut_co = vec![0i16; n_px];
        let mut mut_cg = vec![0i16; n_px];
        ycocg_into(&rgb, &mut mut_y, &mut mut_co, &mut mut_cg);
        assert_ne!(mut_y, ref_y);

        let mut mut_tiles = vec![0u8; n_tiles * 32];
        compute_tiles_into(
            &mut mut_tiles,
            &idx,
            &rgb,
            &ref_luma,
            s,
            gw,
            gh,
            &tx_arr,
            &ty_arr,
            &pad_flags,
            48,
        );
        assert_ne!(mut_tiles, ref_tiles);

        let mut mut_sad = vec![0i64; n_tiles];
        let mut mut_cnt = vec![0i64; n_tiles];
        tile_sad_windows_into(
            &ref_luma,
            &y_prev,
            h,
            w,
            3,
            -2,
            s,
            &tx_i64,
            &ty_i64,
            &mut mut_sad,
            &mut mut_cnt,
        )
        .unwrap();
        assert_ne!(mut_sad, ref_sad);

        let mut mut_sums = vec![0i64; n_tiles];
        tile_sums_i16_into(&ref_luma, s, gh, gw, &mut mut_sums);
        assert_ne!(mut_sums, ref_sums);

        set_active_mutant(MUT_NONE);
        set_threads_override(None).unwrap();
    }
}
