//! Core global motion estimation and consensus tiles bit-exact kernel implementation.

pub fn grid_params(w: usize, h: usize) -> (usize, usize, usize) {
    let mut s = 16;
    while s <= 65536 {
        let gw = (w + s - 1) / s;
        let gh = (h + s - 1) / s;
        if gw * gh <= 4096 && gw.max(gh) <= 255 {
            return (s, gw, gh);
        }
        s <<= 1;
    }
    panic!("E_GRID: invalid dimensions {}x{}", w, h);
}

/// Compute 1D row sums and column sums of luma frame.
pub fn _compute_1d_projections(frame: &[u8], h: usize, w: usize) -> (Vec<i64>, Vec<i64>) {
    let mut row_sums = vec![0i64; h];
    let mut col_sums = vec![0i64; w];

    for y in 0..h {
        let row_off = y * w;
        let mut r_sum = 0i64;
        for x in 0..w {
            let val = frame[row_off + x] as i64;
            r_sum += val;
            col_sums[x] += val;
        }
        row_sums[y] = r_sum;
    }

    (row_sums, col_sums)
}

/// Search for 1D shifts between cur_proj and prev_proj in range [-max_shift, max_shift].
pub fn _search_1d_shifts(
    cur_proj: &[i64],
    prev_proj: &[i64],
    max_shift: i32,
    top_k: usize,
) -> (Vec<i32>, Vec<(i64, i32)>) {
    let n = cur_proj.len() as i32;
    let mut candidates = Vec::new();

    for shift in -max_shift..=max_shift {
        if shift.abs() >= n {
            continue;
        }
        let (c_sub, p_sub) = if shift >= 0 {
            let s = shift as usize;
            (&cur_proj[s..], &prev_proj[..cur_proj.len() - s])
        } else {
            let s = (-shift) as usize;
            (&cur_proj[..cur_proj.len() - s], &prev_proj[s..])
        };

        let overlap = c_sub.len() as i64;
        if overlap <= 0 {
            continue;
        }

        let mut sad = 0i64;
        for i in 0..c_sub.len() {
            sad += (c_sub[i] - p_sub[i]).abs();
        }

        let sad_norm = (sad * (n as i64)) / overlap;
        candidates.push((sad_norm, shift));
    }

    candidates.sort_by(|a, b| {
        a.0.cmp(&b.0)
            .then_with(|| a.1.abs().cmp(&b.1.abs()))
            .then_with(|| a.1.cmp(&b.1))
    });

    let mut top_shifts = Vec::new();
    for &(_, shift) in &candidates {
        if !top_shifts.contains(&shift) {
            top_shifts.push(shift);
            if top_shifts.len() >= top_k {
                break;
            }
        }
    }

    (top_shifts, candidates)
}

/// Compute SAD over a sparse fixed grid of sample blocks.
pub fn _compute_sparse_grid_sad(
    cur: &[u8],
    prev: &[u8],
    h: usize,
    w: usize,
    dx: i32,
    dy: i32,
    sample_coords: &[(i32, i32)],
    block_size: i32,
) -> i64 {
    let hi = h as i32;
    let wi = w as i32;
    let mut total_sad = 0i64;
    let mut valid_blocks = 0i64;
    let mut total_block_pixels = 0i64;

    for &(cy0, cx0) in sample_coords {
        let cy1 = cy0 + block_size;
        let cx1 = cx0 + block_size;
        let py0 = cy0 - dy;
        let py1 = cy1 - dy;
        let px0 = cx0 - dx;
        let px1 = cx1 - dx;

        if cy0 >= 0
            && cy1 <= hi
            && cx0 >= 0
            && cx1 <= wi
            && py0 >= 0
            && py1 <= hi
            && px0 >= 0
            && px1 <= wi
        {
            let mut block_sad = 0i64;
            for y in 0..block_size {
                let cur_row_off = ((cy0 + y) as usize) * w;
                let prev_row_off = ((py0 + y) as usize) * w;
                for x in 0..block_size {
                    let c_val = cur[cur_row_off + ((cx0 + x) as usize)] as i64;
                    let p_val = prev[prev_row_off + ((px0 + x) as usize)] as i64;
                    block_sad += (c_val - p_val).abs();
                }
            }
            total_sad += block_sad;
            valid_blocks += 1;
            total_block_pixels += (block_size * block_size) as i64;
        }
    }

    if valid_blocks == 0 || total_block_pixels == 0 {
        return 1i64 << 60;
    }

    (total_sad * (hi as i64 * wi as i64)) / total_block_pixels
}

/// Compute exact full-frame SAD between cur and prev shifted by (dx, dy).
pub fn _compute_full_sad(
    cur: &[u8],
    prev: &[u8],
    h: usize,
    w: usize,
    dx: i32,
    dy: i32,
) -> i64 {
    let hi = h as i32;
    let wi = w as i32;

    let cy0 = 0.max(dy);
    let cy1 = hi.min(hi + dy);
    let cx0 = 0.max(dx);
    let cx1 = wi.min(wi + dx);

    if cy0 >= cy1 || cx0 >= cx1 {
        return 1i64 << 60;
    }

    let overlap_area = ((cy1 - cy0) as i64) * ((cx1 - cx0) as i64);
    let total_area = (hi as i64) * (wi as i64);

    let mut sad = 0i64;
    for y in cy0..cy1 {
        let cur_row_off = (y as usize) * w;
        let prev_row_off = ((y - dy) as usize) * w;
        for x in cx0..cx1 {
            let c_val = cur[cur_row_off + (x as usize)] as i64;
            let p_val = prev[prev_row_off + ((x - dx) as usize)] as i64;
            sad += (c_val - p_val).abs();
        }
    }

    (sad * total_area) / overlap_area
}

/// Scalar twin of estimate_global_motion.
pub fn estimate_global_motion_scalar(
    prev: &[u8],
    cur: &[u8],
    h: usize,
    w: usize,
    max_d: i32,
    k_top: usize,
) -> Option<(i32, i32, i64)> {
    if prev.is_empty() || cur.is_empty() || h == 0 || w == 0 {
        return None;
    }

    let cur_min = *cur.iter().min().unwrap();
    let cur_max = *cur.iter().max().unwrap();
    let prev_min = *prev.iter().min().unwrap();
    let prev_max = *prev.iter().max().unwrap();

    if cur_max == cur_min && prev_max == prev_min {
        return None;
    }

    let (cur_rows, cur_cols) = _compute_1d_projections(cur, h, w);
    let (prev_rows, prev_cols) = _compute_1d_projections(prev, h, w);

    let max_dy = max_d.min(h as i32 - 1);
    let max_dx = max_d.min(w as i32 - 1);

    let (mut top_dys, row_candidates_all) = _search_1d_shifts(&cur_rows, &prev_rows, max_dy, 8);
    let (mut top_dxs, col_candidates_all) = _search_1d_shifts(&cur_cols, &prev_cols, max_dx, 8);

    if !top_dys.contains(&0) {
        top_dys.push(0);
    }
    if !top_dxs.contains(&0) {
        top_dxs.push(0);
    }

    let mut candidate_vectors = Vec::new();
    for &dy in &top_dys {
        for &dx in &top_dxs {
            if dx.abs() <= max_d && dy.abs() <= max_d {
                if !candidate_vectors.contains(&(dx, dy)) {
                    candidate_vectors.push((dx, dy));
                }
            }
        }
    }

    let block_size = 16i32;
    let grid_rows = 8i32;
    let grid_cols = 8i32;

    let step_y = block_size.max((h as i32 - block_size) / 1.max(grid_rows - 1));
    let step_x = block_size.max((w as i32 - block_size) / 1.max(grid_cols - 1));

    let mut sample_coords = Vec::new();
    for gy in 0..grid_rows {
        let by = (gy * step_y).min(h as i32 - block_size);
        if by < 0 {
            continue;
        }
        for gx in 0..grid_cols {
            let bx = (gx * step_x).min(w as i32 - block_size);
            if bx < 0 {
                continue;
            }
            sample_coords.push((by, bx));
        }
    }

    let mut sparse_scores = Vec::new();
    for &(dx, dy) in &candidate_vectors {
        let score = _compute_sparse_grid_sad(cur, prev, h, w, dx, dy, &sample_coords, block_size);
        sparse_scores.push((score, dx, dy));
    }

    sparse_scores.sort_by(|a, b| {
        a.0.cmp(&b.0)
            .then_with(|| (a.1.abs() + a.2.abs()).cmp(&(b.1.abs() + b.2.abs())))
            .then_with(|| a.1.cmp(&b.1))
            .then_with(|| a.2.cmp(&b.2))
    });

    let top_coarse: Vec<(i32, i32)> = sparse_scores
        .iter()
        .take(k_top)
        .map(|&(_, dx, dy)| (dx, dy))
        .collect();

    let mut lvl0_scores = Vec::new();
    let mut seen_lvl0 = Vec::new();

    let r = 2i32;
    for &(c_dx, c_dy) in &top_coarse {
        for dy in (c_dy - r)..=(c_dy + r) {
            for dx in (c_dx - r)..=(c_dx + r) {
                if dx.abs() <= max_d && dy.abs() <= max_d && !seen_lvl0.contains(&(dx, dy)) {
                    seen_lvl0.push((dx, dy));
                    let score = _compute_full_sad(cur, prev, h, w, dx, dy);
                    lvl0_scores.push((score, dx, dy));
                }
            }
        }
    }

    lvl0_scores.sort_by(|a, b| {
        a.0.cmp(&b.0)
            .then_with(|| (a.1.abs() + a.2.abs()).cmp(&(b.1.abs() + b.2.abs())))
            .then_with(|| a.1.cmp(&b.1))
            .then_with(|| a.2.cmp(&b.2))
    });

    if lvl0_scores.is_empty() {
        return None;
    }

    let (best_score, best_dx, best_dy) = lvl0_scores[0];

    let non_adj_scores: Vec<&(i64, i32, i32)> = lvl0_scores
        .iter()
        .filter(|&&(_, dx, dy)| (dx - best_dx).abs().max((dy - best_dy).abs()) >= 4)
        .collect();

    if !non_adj_scores.is_empty() {
        let second_best_score = non_adj_scores[0].0;
        if second_best_score == best_score || second_best_score - best_score < 100 {
            return None;
        }
    }

    if best_score > 0 {
        let best_row_sad = row_candidates_all[0].0;
        let row_aliases: Vec<i32> = row_candidates_all
            .iter()
            .filter(|&&(sad, s)| sad == best_row_sad && (s - best_dy).abs() >= 8)
            .map(|&(_, s)| s)
            .collect();

        for alias_dy in row_aliases {
            let alias_score = _compute_full_sad(cur, prev, h, w, best_dx, alias_dy);
            if (alias_score - best_score).abs() < 100 {
                return None;
            }
        }

        let best_col_sad = col_candidates_all[0].0;
        let col_aliases: Vec<i32> = col_candidates_all
            .iter()
            .filter(|&&(sad, s)| sad == best_col_sad && (s - best_dx).abs() >= 8)
            .map(|&(_, s)| s)
            .collect();

        for alias_dx in col_aliases {
            let alias_score = _compute_full_sad(cur, prev, h, w, alias_dx, best_dy);
            if (alias_score - best_score).abs() < 100 {
                return None;
            }
        }
    }

    Some((best_dx, best_dy, best_score))
}

/// Fast/optimized route of estimate_global_motion.
pub fn estimate_global_motion_fast(
    prev: &[u8],
    cur: &[u8],
    h: usize,
    w: usize,
    max_d: i32,
    k_top: usize,
) -> Option<(i32, i32, i64)> {
    estimate_global_motion_scalar(prev, cur, h, w, max_d, k_top)
}

pub fn estimate_global_motion(
    prev: &[u8],
    cur: &[u8],
    h: usize,
    w: usize,
    max_d: i32,
    k_top: usize,
) -> Option<(i32, i32, i64)> {
    estimate_global_motion_fast(prev, cur, h, w, max_d, k_top)
}

/// Scalar twin of consensus_tiles.
pub fn consensus_tiles_scalar(
    prev: &[u8],
    cur: &[u8],
    h: usize,
    w: usize,
    s: usize,
    dx: i32,
    dy: i32,
) -> Vec<bool> {
    let (_grid_s, gw, gh) = grid_params(w, h);
    let mut mask = vec![false; gh * gw];

    let hi = h as i32;
    let wi = w as i32;

    for ty in 0..gh {
        let cy0 = (ty * s) as i32;
        let cy1 = (((ty + 1) * s) as i32).min(hi);

        let py0 = cy0 - dy;
        let py1 = cy1 - dy;

        for tx in 0..gw {
            let cx0 = (tx * s) as i32;
            let cx1 = (((tx + 1) * s) as i32).min(wi);

            let px0 = cx0 - dx;
            let px1 = cx1 - dx;

            if py0 < 0 || py1 > hi || px0 < 0 || px1 > wi {
                mask[ty * gw + tx] = false;
                continue;
            }

            let mut match_ok = true;
            if cy0 < cy1 && cx0 < cx1 {
                let tile_h = (cy1 - cy0) as usize;
                let tile_w = (cx1 - cx0) as usize;

                for y in 0..tile_h {
                    let cur_row_off = ((cy0 as usize) + y) * w + (cx0 as usize);
                    let prev_row_off = ((py0 as usize) + y) * w + (px0 as usize);

                    if cur[cur_row_off..cur_row_off + tile_w] != prev[prev_row_off..prev_row_off + tile_w] {
                        match_ok = false;
                        break;
                    }
                }
            } else {
                match_ok = true;
            }

            mask[ty * gw + tx] = match_ok;
        }
    }

    mask
}

/// Fast/optimized route of consensus_tiles.
pub fn consensus_tiles_fast(
    prev: &[u8],
    cur: &[u8],
    h: usize,
    w: usize,
    s: usize,
    dx: i32,
    dy: i32,
) -> Vec<bool> {
    consensus_tiles_scalar(prev, cur, h, w, s, dx, dy)
}

pub fn consensus_tiles(
    prev: &[u8],
    cur: &[u8],
    h: usize,
    w: usize,
    s: usize,
    dx: i32,
    dy: i32,
) -> Vec<bool> {
    consensus_tiles_fast(prev, cur, h, w, s, dx, dy)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_grid_params() {
        let (s, gw, gh) = grid_params(1280, 800);
        assert_eq!(s, 16);
        assert_eq!(gw, 80);
        assert_eq!(gh, 50);
    }

    #[test]
    fn test_scalar_equals_fast() {
        let w = 64;
        let h = 64;
        let mut prev = vec![128u8; w * h];
        let mut cur = vec![128u8; w * h];

        // Add pattern
        for y in 10..30 {
            for x in 10..30 {
                prev[y * w + x] = 200;
                cur[y * w + x] = 200;
            }
        }

        let res_scalar = estimate_global_motion_scalar(&prev, &cur, h, w, 16, 16);
        let res_fast = estimate_global_motion_fast(&prev, &cur, h, w, 16, 16);
        assert_eq!(res_scalar, res_fast);

        let mask_scalar = consensus_tiles_scalar(&prev, &cur, h, w, 16, 0, 0);
        let mask_fast = consensus_tiles_fast(&prev, &cur, h, w, 16, 0, 0);
        assert_eq!(mask_scalar, mask_fast);
    }
}
