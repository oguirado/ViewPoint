//! 4x4 block subtile motion kernel for V2 post-pass (st_block4).

pub const TILE_MIN_PX: i32 = 16;
pub const CELL_MAX: i32 = 4096;
pub const AXIS_MAX: i32 = 255;
pub const DIM_MAX: i32 = 8192;

/// Calculate grid parameters (s, gw, gh) bit-exactly matching visioncore.layout.grid_params.
pub fn grid_params(w: i32, h: i32) -> Result<(i32, i32, i32), &'static str> {
    if w < 1 || w > DIM_MAX || h < 1 || h > DIM_MAX {
        return Err("dimension outside 1..8192");
    }
    let mut s = TILE_MIN_PX;
    while s <= (1 << 16) {
        let gw = (w + s - 1) / s;
        let gh = (h + s - 1) / s;
        if gw * gh <= CELL_MAX && gw.max(gh) <= AXIS_MAX {
            return Ok((s, gw, gh));
        }
        s <<= 1;
    }
    Err("E_GRID")
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Candidate {
    pub dx: i32,
    pub dy: i32,
    pub dist2: i32,
}

pub fn generate_candidates(max_d: i32) -> Vec<Candidate> {
    let mut candidates = Vec::new();
    for dx in -max_d..=max_d {
        for dy in -max_d..=max_d {
            if dx == 0 && dy == 0 {
                continue;
            }
            candidates.push(Candidate {
                dx,
                dy,
                dist2: dx * dx + dy * dy,
            });
        }
    }
    // Sort key: (dx^2 + dy^2, abs(dx), abs(dy), dx, dy)
    candidates.sort_by(|a, b| {
        a.dist2
            .cmp(&b.dist2)
            .then_with(|| a.dx.abs().cmp(&b.dx.abs()))
            .then_with(|| a.dy.abs().cmp(&b.dy.abs()))
            .then_with(|| a.dx.cmp(&b.dx))
            .then_with(|| a.dy.cmp(&b.dy))
    });
    candidates
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SubtileResult {
    pub dx: i32,
    pub dy: i32,
    pub n_blocks: i32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BlockRect {
    pub bx: i32,
    pub by: i32,
    pub bx_end: i32,
    pub by_end: i32,
}

/// Helper to get padded pixel from prev_luma using mode="edge".
#[inline(always)]
fn get_padded_pixel(prev_luma: &[u8], w: i32, h: i32, x: i32, y: i32) -> u8 {
    let clamped_x = x.clamp(0, w - 1) as usize;
    let clamped_y = y.clamp(0, h - 1) as usize;
    prev_luma[clamped_y * (w as usize) + clamped_x]
}

/// Plain scalar twin reproducing eval/v2x/st_block4.py logic bit-exactly.
pub fn subtile_motion_scalar(
    prev_luma: &[u8],
    cur_luma: &[u8],
    w: i32,
    h: i32,
    s_opt: Option<i32>,
    tile_xy: (i32, i32),
    max_d: i32,
) -> Option<SubtileResult> {
    let (s_grid, gw, gh) = grid_params(w, h).ok()?;
    let s = s_opt.unwrap_or(s_grid);

    let (tx, ty) = tile_xy;
    if tx < 0 || tx >= gw || ty < 0 || ty >= gh {
        return None;
    }

    let tx_min = (tx - 1).max(0);
    let tx_max = (tx + 1).min(gw - 1);
    let ty_min = (ty - 1).max(0);
    let ty_max = (ty + 1).min(gh - 1);

    let x0 = tx_min * s;
    let x1 = w.min((tx_max + 1) * s);
    let y0 = ty_min * s;
    let y1 = h.min((ty_max + 1) * s);

    let mut changed_blocks = Vec::new();
    let mut by = y0;
    while by < y1 {
        let by_end = h.min(by + 4);
        let mut bx = x0;
        while bx < x1 {
            let bx_end = w.min(bx + 4);

            let mut diff = false;
            'check_blk: for r in by..by_end {
                let row_off = (r as usize) * (w as usize);
                for c in bx..bx_end {
                    let idx = row_off + (c as usize);
                    if cur_luma[idx] != prev_luma[idx] {
                        diff = true;
                        break 'check_blk;
                    }
                }
            }

            if diff {
                changed_blocks.push(BlockRect {
                    bx,
                    by,
                    bx_end,
                    by_end,
                });
            }
            bx += 4;
        }
        by += 4;
    }

    let n_blocks = changed_blocks.len() as i32;
    if n_blocks == 0 {
        return None;
    }

    let ty0 = ty * s;
    let ty1 = h.min((ty + 1) * s);
    let tx0 = tx * s;
    let tx1 = w.min((tx + 1) * s);

    // Build explicit padded prev_luma for scalar twin to mirror np.pad(mode="edge")
    let pad_w = w + 2 * max_d;
    let pad_h = h + 2 * max_d;
    let mut prev_padded = vec![0u8; (pad_w * pad_h) as usize];
    for py in 0..pad_h {
        let orig_y = (py - max_d).clamp(0, h - 1) as usize;
        for px in 0..pad_w {
            let orig_x = (px - max_d).clamp(0, w - 1) as usize;
            prev_padded[(py * pad_w + px) as usize] = prev_luma[orig_y * (w as usize) + orig_x];
        }
    }

    let candidates = generate_candidates(max_d);

    for cand in &candidates {
        let dx = cand.dx;
        let dy = cand.dy;
        let mut match_all = true;

        for blk in &changed_blocks {
            let py0 = blk.by - dy + max_d;
            let px0 = blk.bx - dx + max_d;
            let bh = blk.by_end - blk.by;
            let bw = blk.bx_end - blk.bx;

            for r in 0..bh {
                let cur_row_off = ((blk.by + r) as usize) * (w as usize) + (blk.bx as usize);
                let pad_row_off = ((py0 + r) as usize) * (pad_w as usize) + (px0 as usize);
                if cur_luma[cur_row_off..cur_row_off + (bw as usize)]
                    != prev_padded[pad_row_off..pad_row_off + (bw as usize)]
                {
                    match_all = false;
                    break;
                }
            }
            if !match_all {
                break;
            }
        }

        if match_all {
            // EDGE_RULE Check 1: Target tile must not equal prev_luma at zero shift
            let tile_h = ty1 - ty0;
            let tile_w = tx1 - tx0;
            let mut zero_shift_equal = true;
            for r in 0..tile_h {
                let row_off = ((ty0 + r) as usize) * (w as usize) + (tx0 as usize);
                let cur_row = &cur_luma[row_off..row_off + (tile_w as usize)];
                let prev_row = &prev_luma[row_off..row_off + (tile_w as usize)];
                if cur_row != prev_row {
                    zero_shift_equal = false;
                    break;
                }
            }
            if zero_shift_equal {
                return None;
            }

            // EDGE_RULE Check 2: Target tile source in prev_luma must lie inside frame
            let spy0 = ty0 - dy;
            let spy1 = ty1 - dy;
            let spx0 = tx0 - dx;
            let spx1 = tx1 - dx;
            if spy0 < 0 || spy1 > h || spx0 < 0 || spx1 > w {
                return None;
            }

            // EDGE_RULE Check 3: Target tile must equal translated prev_luma
            let mut trans_equal = true;
            for r in 0..tile_h {
                let cur_row_off = ((ty0 + r) as usize) * (w as usize) + (tx0 as usize);
                let prev_row_off = ((spy0 + r) as usize) * (w as usize) + (spx0 as usize);
                let cur_row = &cur_luma[cur_row_off..cur_row_off + (tile_w as usize)];
                let prev_row = &prev_luma[prev_row_off..prev_row_off + (tile_w as usize)];
                if cur_row != prev_row {
                    trans_equal = false;
                    break;
                }
            }

            if trans_equal {
                return Some(SubtileResult {
                    dx,
                    dy,
                    n_blocks,
                });
            } else {
                return None;
            }
        }
    }

    None
}

/// Optimized subtile_motion route.
pub fn subtile_motion_opt(
    prev_luma: &[u8],
    cur_luma: &[u8],
    w: i32,
    h: i32,
    s_opt: Option<i32>,
    tile_xy: (i32, i32),
    max_d: i32,
) -> Option<SubtileResult> {
    let (s_grid, gw, gh) = grid_params(w, h).ok()?;
    let s = s_opt.unwrap_or(s_grid);

    let (tx, ty) = tile_xy;
    if tx < 0 || tx >= gw || ty < 0 || ty >= gh {
        return None;
    }

    let tx_min = (tx - 1).max(0);
    let tx_max = (tx + 1).min(gw - 1);
    let ty_min = (ty - 1).max(0);
    let ty_max = (ty + 1).min(gh - 1);

    let x0 = tx_min * s;
    let x1 = w.min((tx_max + 1) * s);
    let y0 = ty_min * s;
    let y1 = h.min((ty_max + 1) * s);

    let pitch = w as usize;

    let mut changed_blocks = Vec::new();
    let mut by = y0;
    while by < y1 {
        let by_end = h.min(by + 4);
        let bh = (by_end - by) as usize;
        let mut bx = x0;
        while bx < x1 {
            let bx_end = w.min(bx + 4);
            let bw = (bx_end - bx) as usize;

            let mut diff = false;
            for r in 0..bh {
                let off = ((by as usize) + r) * pitch + (bx as usize);
                if cur_luma[off..off + bw] != prev_luma[off..off + bw] {
                    diff = true;
                    break;
                }
            }

            if diff {
                changed_blocks.push(BlockRect {
                    bx,
                    by,
                    bx_end,
                    by_end,
                });
            }
            bx += 4;
        }
        by += 4;
    }

    let n_blocks = changed_blocks.len() as i32;
    if n_blocks == 0 {
        return None;
    }

    let candidates = generate_candidates(max_d);

    let ty0 = ty * s;
    let ty1 = h.min((ty + 1) * s);
    let tx0 = tx * s;
    let tx1 = w.min((tx + 1) * s);

    let tile_h = (ty1 - ty0) as usize;
    let tile_w = (tx1 - tx0) as usize;

    for cand in &candidates {
        let dx = cand.dx;
        let dy = cand.dy;
        let mut match_all = true;

        for blk in &changed_blocks {
            let bh = (blk.by_end - blk.by) as usize;
            let bw = (blk.bx_end - blk.bx) as usize;

            for r in 0..bh {
                let cur_y = blk.by + (r as i32);
                let cur_off = (cur_y as usize) * pitch + (blk.bx as usize);
                let cur_slice = &cur_luma[cur_off..cur_off + bw];

                // Fast unpadded slice check if source block is fully inside frame
                let src_y = cur_y - dy;
                let src_x = blk.bx - dx;

                if src_y >= 0 && src_y + (bh as i32) <= h && src_x >= 0 && src_x + (bw as i32) <= w {
                    let prev_off = (src_y as usize) * pitch + (src_x as usize);
                    if cur_slice != &prev_luma[prev_off..prev_off + bw] {
                        match_all = false;
                        break;
                    }
                } else {
                    // Padded lookup pixel by pixel
                    let mut row_match = true;
                    for c in 0..bw {
                        let px = src_x + (c as i32);
                        let py = src_y;
                        let p_val = get_padded_pixel(prev_luma, w, h, px, py);
                        if cur_slice[c] != p_val {
                            row_match = false;
                            break;
                        }
                    }
                    if !row_match {
                        match_all = false;
                        break;
                    }
                }
            }

            if !match_all {
                break;
            }
        }

        if match_all {
            // EDGE_RULE 1: Target tile must not equal zero shift
            let mut zero_equal = true;
            for r in 0..tile_h {
                let off = ((ty0 as usize) + r) * pitch + (tx0 as usize);
                if cur_luma[off..off + tile_w] != prev_luma[off..off + tile_w] {
                    zero_equal = false;
                    break;
                }
            }
            if zero_equal {
                return None;
            }

            // EDGE_RULE 2: Target tile source in prev_luma must lie inside frame
            let spy0 = ty0 - dy;
            let spy1 = ty1 - dy;
            let spx0 = tx0 - dx;
            let spx1 = tx1 - dx;
            if spy0 < 0 || spy1 > h || spx0 < 0 || spx1 > w {
                return None;
            }

            // EDGE_RULE 3: Target tile must equal translated prev_luma
            let mut trans_equal = true;
            for r in 0..tile_h {
                let cur_off = ((ty0 as usize) + r) * pitch + (tx0 as usize);
                let prev_off = ((spy0 as usize) + r) * pitch + (spx0 as usize);
                if cur_luma[cur_off..cur_off + tile_w] != prev_luma[prev_off..prev_off + tile_w] {
                    trans_equal = false;
                    break;
                }
            }

            if trans_equal {
                return Some(SubtileResult {
                    dx,
                    dy,
                    n_blocks,
                });
            } else {
                return None;
            }
        }
    }

    None
}

pub fn subtile_motion_batch(
    prev_luma: &[u8],
    cur_luma: &[u8],
    w: i32,
    h: i32,
    s: i32,
    max_d: i32,
    tiles: &[(i32, i32)],
) -> Vec<Option<SubtileResult>> {
    tiles
        .iter()
        .map(|&tile_xy| subtile_motion_opt(prev_luma, cur_luma, w, h, Some(s), tile_xy, max_d))
        .collect()
}
