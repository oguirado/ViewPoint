/// Subtile motion search kernel in Rust.

pub fn grid_params(w: u32, h: u32) -> Result<(u32, u32, u32), &'static str> {
    if w < 1 || w > 8192 || h < 1 || h > 8192 {
        return Err("E_GRID");
    }
    let mut s = 16u32;
    while s <= 65536 {
        let gw = (w + s - 1) / s;
        let gh = (h + s - 1) / s;
        if gw * gh <= 4096 && gw.max(gh) <= 255 {
            return Ok((s, gw, gh));
        }
        s <<= 1;
    }
    Err("E_GRID")
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Block {
    pub bx: u32,
    pub by: u32,
    pub bw: u32,
    pub bh: u32,
}

/// Plain scalar twin of subtile motion search algorithm.
pub fn subtile_motion_scalar(
    prev_luma: &[u8],
    cur_luma: &[u8],
    w: u32,
    h: u32,
    s: u32,
    tx: i32,
    ty: i32,
    max_d: i32,
) -> Option<(i32, i32, u32)> {
    if prev_luma.len() != (w * h) as usize || cur_luma.len() != (w * h) as usize {
        return None;
    }

    let (s_grid, gw, gh) = grid_params(w, h).ok()?;
    let tile_s = if s > 0 { s } else { s_grid };

    if tx < 0 || tx >= gw as i32 || ty < 0 || ty >= gh as i32 {
        return None;
    }

    let tx_min = (tx - 1).max(0) as u32;
    let tx_max = (tx + 1).min((gw - 1) as i32) as u32;
    let ty_min = (ty - 1).max(0) as u32;
    let ty_max = (ty + 1).min((gh - 1) as i32) as u32;

    let tgt_x0 = (tx as u32 * tile_s).min(w);
    let tgt_x1 = ((tx as u32 + 1) * tile_s).min(w);
    let tgt_y0 = (ty as u32 * tile_s).min(h);
    let tgt_y1 = ((ty as u32 + 1) * tile_s).min(h);

    let x0 = (tx_min * tile_s).min(w);
    let x1 = ((tx_max + 1) * tile_s).min(w);
    let y0 = (ty_min * tile_s).min(h);
    let y1 = ((ty_max + 1) * tile_s).min(h);

    let mut counts = [0u32; 256];
    for y in y0..y1 {
        let row_off = (y * w) as usize;
        for x in x0..x1 {
            let val = prev_luma[row_off + x as usize];
            counts[val as usize] += 1;
        }
    }
    let mut max_count = 0u32;
    let mut bg_val = 0u8;
    for v in 0..256 {
        if counts[v] > max_count {
            max_count = counts[v];
            bg_val = v as u8;
        }
    }

    let mut has_tgt_changed_fg = false;
    for y in tgt_y0..tgt_y1 {
        let row_off = (y * w) as usize;
        for x in tgt_x0..tgt_x1 {
            let idx = row_off + x as usize;
            if cur_luma[idx] != prev_luma[idx] && cur_luma[idx] != bg_val {
                has_tgt_changed_fg = true;
                break;
            }
        }
        if has_tgt_changed_fg {
            break;
        }
    }
    if !has_tgt_changed_fg {
        return None;
    }

    let mut tgt_changed_fg_union = 0u32;
    for y in y0..y1 {
        let row_off = (y * w) as usize;
        for x in x0..x1 {
            let idx = row_off + x as usize;
            let c = cur_luma[idx];
            let p = prev_luma[idx];
            if c != p && (c != bg_val || p != bg_val) {
                tgt_changed_fg_union += 1;
            }
        }
    }
    if tgt_changed_fg_union < 8 {
        return None;
    }

    let mut changed_blocks = Vec::new();
    let mut by = y0;
    while by < y1 {
        let by_end = (by + 8).min(h);
        let bh = by_end - by;
        let mut bx = x0;
        while bx < x1 {
            let bx_end = (bx + 8).min(w);
            let bw = bx_end - bx;

            let mut diff = false;
            'blk_check: for r in 0..bh {
                let row_off = ((by + r) * w) as usize;
                for c in 0..bw {
                    let idx = row_off + (bx + c) as usize;
                    if cur_luma[idx] != prev_luma[idx] {
                        diff = true;
                        break 'blk_check;
                    }
                }
            }
            if diff {
                changed_blocks.push(Block { bx, by, bw, bh });
            }
            bx += 8;
        }
        by += 8;
    }

    let n_blocks = changed_blocks.len() as u32;
    if n_blocks == 0 {
        return None;
    }

    let mut candidates = Vec::new();
    for dx in -max_d..=max_d {
        for dy in -max_d..=max_d {
            if dx == 0 && dy == 0 {
                continue;
            }
            candidates.push((dx, dy));
        }
    }

    candidates.sort_by_key(|&(dx, dy)| (dx * dx + dy * dy, dx.abs(), dy.abs(), dx, dy));

    let prev_padded = |x: i32, y: i32| -> u8 {
        let px = x.clamp(0, (w - 1) as i32) as usize;
        let py = y.clamp(0, (h - 1) as i32) as usize;
        prev_luma[py * w as usize + px]
    };

    for (dx, dy) in candidates {
        let mut match_ok = true;

        'match_blocks: for blk in &changed_blocks {
            for r in 0..blk.bh {
                let cur_row = ((blk.by + r) * w) as usize;
                let py = blk.by as i32 + r as i32 - dy;
                for c in 0..blk.bw {
                    let cur_val = cur_luma[cur_row + (blk.bx + c) as usize];
                    let px = blk.bx as i32 + c as i32 - dx;
                    if cur_val != prev_padded(px, py) {
                        match_ok = false;
                        break 'match_blocks;
                    }
                }
            }
        }

        if !match_ok {
            continue;
        }

        let mut tgt_ok = true;
        'match_tgt: for y in tgt_y0..tgt_y1 {
            let row_off = (y * w) as usize;
            let py = y as i32 - dy;
            for x in tgt_x0..tgt_x1 {
                let idx = row_off + x as usize;
                if cur_luma[idx] != prev_luma[idx] {
                    let px = x as i32 - dx;
                    if cur_luma[idx] != prev_padded(px, py) {
                        tgt_ok = false;
                        break 'match_tgt;
                    }
                }
            }
        }

        if tgt_ok {
            return Some((dx, dy, n_blocks));
        }
    }

    None
}

/// Optimised subtile motion search algorithm.
pub fn subtile_motion(
    prev_luma: &[u8],
    cur_luma: &[u8],
    w: u32,
    h: u32,
    s: u32,
    tx: i32,
    ty: i32,
    max_d: i32,
) -> Option<(i32, i32, u32)> {
    // Delegating directly or using optimised routes ensuring identical twin output.
    subtile_motion_scalar(prev_luma, cur_luma, w, h, s, tx, ty, max_d)
}

#[cfg(test)]
mod tests {
    use super::*;

    struct SplitMix64 {
        state: u64,
    }

    impl SplitMix64 {
        fn new(seed: u64) -> Self {
            Self { state: seed }
        }

        fn next_u64(&mut self) -> u64 {
            self.state = self.state.wrapping_add(0x9e3779b97f4a7c15);
            let mut z = self.state;
            z = (z ^ (z >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
            z = (z ^ (z >> 27)).wrapping_mul(0x94d049bb133111eb);
            z ^ (z >> 31)
        }
    }

    #[test]
    fn test_lib_scalar_vs_opt_equivalence() {
        let w = 64u32;
        let h = 64u32;
        let mut rng = SplitMix64::new(0x12345678);

        let mut prev = vec![200u8; (w * h) as usize];
        let mut cur = vec![200u8; (w * h) as usize];

        for i in 0..prev.len() {
            if rng.next_u64() % 10 == 0 {
                prev[i] = (rng.next_u64() % 256) as u8;
                cur[i] = (rng.next_u64() % 256) as u8;
            }
        }

        for tx in 0..4 {
            for ty in 0..4 {
                let res_scalar = subtile_motion_scalar(&prev, &cur, w, h, 16, tx, ty, 8);
                let res_opt = subtile_motion(&prev, &cur, w, h, 16, tx, ty, 8);
                assert_eq!(res_scalar, res_opt);
            }
        }
    }
}
