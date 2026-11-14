// rust/vqa_k_s2_motion/src/lib.rs

pub const MOTION_D_MAX: i32 = 64;

/// Best integer shift d (cur[x] ~ prev[x-d]) of two 1-D projections; 0 if no clear winner.
pub fn axis_shift_scalar(cur: &[i64], prev: &[i64], dmax: i32) -> i32 {
    let n = cur.len() as i64;
    if n == 0 {
        return 0;
    }

    let mut best: Option<(i64, i64, i32)> = None;
    let mut cost0: Option<i64> = None;

    for d in -dmax..=dmax {
        if d.abs() as i64 >= n {
            continue;
        }

        let sad: i64 = if d >= 0 {
            let d_us = d as usize;
            let n_us = n as usize;
            cur[d_us..n_us]
                .iter()
                .zip(&prev[0..n_us - d_us])
                .map(|(&c, &p)| (c - p).abs())
                .sum()
        } else {
            let abs_d = (-d) as usize;
            let n_us = n as usize;
            cur[0..n_us - abs_d]
                .iter()
                .zip(&prev[abs_d..n_us])
                .map(|(&c, &p)| (c - p).abs())
                .sum()
        };

        let denom = n - d.abs() as i64;
        let cost = (sad * n) / denom;

        if d == 0 {
            cost0 = Some(cost);
        }

        let key = (cost, d.abs() as i64, d);
        match best {
            None => best = Some(key),
            Some(b) => {
                if key < b {
                    best = Some(key);
                }
            }
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

pub fn axis_shift(cur: &[i64], prev: &[i64], dmax: i32) -> i32 {
    axis_shift_scalar(cur, prev, dmax)
}

/// Global vector (gdx, gdy) from row/column projections.
pub fn global_vector_scalar(
    y_cur: &[i16],
    y_prev: &[i16],
    width: usize,
    height: usize,
    s: usize,
) -> (i32, i32) {
    if width == 0 || height == 0 || y_cur.len() < width * height || y_prev.len() < width * height {
        return (0, 0);
    }
    let dmax = std::cmp::min(s, MOTION_D_MAX as usize) as i32;

    // Column sums (horizontal projection)
    let mut cur_col = vec![0i64; width];
    let mut prev_col = vec![0i64; width];
    for y in 0..height {
        let row_off = y * width;
        for x in 0..width {
            cur_col[x] += y_cur[row_off + x] as i64;
            prev_col[x] += y_prev[row_off + x] as i64;
        }
    }

    // Row sums (vertical projection)
    let mut cur_row = vec![0i64; height];
    let mut prev_row = vec![0i64; height];
    for y in 0..height {
        let row_off = y * width;
        let mut sum_c = 0i64;
        let mut sum_p = 0i64;
        for x in 0..width {
            sum_c += y_cur[row_off + x] as i64;
            sum_p += y_prev[row_off + x] as i64;
        }
        cur_row[y] = sum_c;
        prev_row[y] = sum_p;
    }

    let gdx = axis_shift_scalar(&cur_col, &prev_col, dmax);
    let gdy = axis_shift_scalar(&cur_row, &prev_row, dmax);
    (gdx, gdy)
}

pub fn global_vector(
    y_cur: &[i16],
    y_prev: &[i16],
    width: usize,
    height: usize,
    s: usize,
) -> (i32, i32) {
    global_vector_scalar(y_cur, y_prev, width, height, s)
}

/// Sum absdiff over tile grid (gh, gw) of size s.
pub fn tile_sums_scalar(absdiff: &[i64], width: usize, height: usize, s: usize) -> Vec<i64> {
    let gw = width / s;
    let gh = height / s;
    let mut sums = vec![0i64; gh * gw];

    for ty in 0..gh {
        for tx in 0..gw {
            let mut sum = 0i64;
            for py in 0..s {
                let y = ty * s + py;
                if y >= height {
                    continue;
                }
                let row_off = y * width;
                for px in 0..s {
                    let x = tx * s + px;
                    if x >= width {
                        continue;
                    }
                    sum += absdiff[row_off + x];
                }
            }
            sums[ty * gw + tx] = sum;
        }
    }
    sums
}

pub fn tile_sums(absdiff: &[i64], width: usize, height: usize, s: usize) -> Vec<i64> {
    tile_sums_scalar(absdiff, width, height, s)
}

/// 8-connected labelling of a bool (gh, gw) mask in raster order of first tile.
pub fn islands_scalar(mask: &[bool], gh: usize, gw: usize) -> (Vec<i32>, i32) {
    let mut lab = vec![0i32; gh * gw];
    let mut n = 0i32;

    for y0 in 0..gh {
        for x0 in 0..gw {
            let idx0 = y0 * gw + x0;
            if !mask[idx0] || lab[idx0] != 0 {
                continue;
            }
            n += 1;
            lab[idx0] = n;
            let mut stack = vec![(y0, x0)];

            while let Some((y, x)) = stack.pop() {
                let y_min = if y == 0 { 0 } else { y - 1 };
                let y_max = std::cmp::min(gh, y + 2);
                let x_min = if x == 0 { 0 } else { x - 1 };
                let x_max = std::cmp::min(gw, x + 2);

                for yy in y_min..y_max {
                    for xx in x_min..x_max {
                        let idx = yy * gw + xx;
                        if mask[idx] && lab[idx] == 0 {
                            lab[idx] = n;
                            stack.push((yy, xx));
                        }
                    }
                }
            }
        }
    }

    (lab, n)
}

pub fn islands(mask: &[bool], gh: usize, gw: usize) -> (Vec<i32>, i32) {
    islands_scalar(mask, gh, gw)
}

/// (dx, dy) of one island (bool (gh, gw) tile mask): projections over its bbox + s margin.
pub fn island_vector_scalar(
    lab_mask: &[bool],
    y_cur: &[i16],
    y_prev: &[i16],
    width: usize,
    height: usize,
    s: usize,
    gh: usize,
    gw: usize,
) -> (i32, i32) {
    let mut ys_min = usize::MAX;
    let mut ys_max = 0usize;
    let mut xs_min = usize::MAX;
    let mut xs_max = 0usize;
    let mut found = false;

    for ty in 0..gh {
        for tx in 0..gw {
            if lab_mask[ty * gw + tx] {
                found = true;
                if ty < ys_min {
                    ys_min = ty;
                }
                if ty > ys_max {
                    ys_max = ty;
                }
                if tx < xs_min {
                    xs_min = tx;
                }
                if tx > xs_max {
                    xs_max = tx;
                }
            }
        }
    }

    if !found {
        return (0, 0);
    }

    let r0 = if ys_min == 0 { 0 } else { (ys_min - 1) * s };
    let r1 = std::cmp::min(height, (ys_max + 2) * s);
    let c0 = if xs_min == 0 { 0 } else { (xs_min - 1) * s };
    let c1 = std::cmp::min(width, (xs_max + 2) * s);

    if r0 >= r1 || c0 >= c1 {
        return (0, 0);
    }

    let sub_w = c1 - c0;
    let sub_h = r1 - r0;
    let mut sub_cur = vec![0i16; sub_w * sub_h];
    let mut sub_prev = vec![0i16; sub_w * sub_h];

    for sy in 0..sub_h {
        let src_y = r0 + sy;
        let src_off = src_y * width + c0;
        let dst_off = sy * sub_w;
        sub_cur[dst_off..dst_off + sub_w].copy_from_slice(&y_cur[src_off..src_off + sub_w]);
        sub_prev[dst_off..dst_off + sub_w].copy_from_slice(&y_prev[src_off..src_off + sub_w]);
    }

    global_vector_scalar(&sub_cur, &sub_prev, sub_w, sub_h, s)
}

pub fn island_vector(
    lab_mask: &[bool],
    y_cur: &[i16],
    y_prev: &[i16],
    width: usize,
    height: usize,
    s: usize,
    gh: usize,
    gw: usize,
) -> (i32, i32) {
    island_vector_scalar(lab_mask, y_cur, y_prev, width, height, s, gh, gw)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_axis_shift_twin() {
        let cur = vec![10, 20, 30, 40, 50];
        let prev = vec![5, 10, 20, 30, 40];
        assert_eq!(
            axis_shift(&cur, &prev, 4),
            axis_shift_scalar(&cur, &prev, 4)
        );
    }

    #[test]
    fn test_islands_twin() {
        let mask = vec![
            true, false, true,
            false, false, true,
            true, true, false,
        ];
        let (lab_opt, n_opt) = islands(&mask, 3, 3);
        let (lab_s, n_s) = islands_scalar(&mask, 3, 3);
        assert_eq!(lab_opt, lab_s);
        assert_eq!(n_opt, n_s);
    }
}
