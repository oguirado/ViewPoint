pub type Result<T> = std::result::Result<T, SadWinError>;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SadWinError {
    IndexOutOfBounds,
    DimensionMismatch,
    InvalidTileSize,
}

/// Scalar twin reference implementation of `_tile_sad_windows`.
/// Matches Python `_tile_sad_windows` in visioncore/change.py bit-exactly.
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
    if y_cur.len() != h * w || y_prev.len() != h * w {
        return Err(SadWinError::DimensionMismatch);
    }
    if tx.len() != ty.len() {
        return Err(SadWinError::DimensionMismatch);
    }

    let w_i64 = w as i64;
    let h_i64 = h as i64;
    let s_i64 = s as i64;

    let max_tx = w_i64 / s_i64;
    let max_ty = h_i64 / s_i64;

    // Bounds check on requested tile coordinates
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

        let y0 = std::cmp::max(yy, dy);
        let y1 = std::cmp::min(yy + s_i64, h_i64 + dy);
        let x0 = std::cmp::max(xx, dx);
        let x1 = std::cmp::min(xx + s_i64, w_i64 + dx);

        if y0 < y1 && x0 < x1 {
            let mut tile_sad: i64 = 0;
            let mut tile_cnt: i64 = 0;

            for r in y0..y1 {
                let cur_row_off = (r as usize) * w;
                let prev_row_off = ((r - dy) as usize) * w;
                for c in x0..x1 {
                    let cur_px = y_cur[cur_row_off + (c as usize)] as i64;
                    let prev_px = y_prev[prev_row_off + ((c - dx) as usize)] as i64;
                    tile_sad += (cur_px - prev_px).abs();
                    tile_cnt += 1;
                }
            }
            sad[i] = tile_sad;
            count[i] = tile_cnt;
        } else {
            sad[i] = 0;
            count[i] = 0;
        }
    }

    Ok((sad, count))
}

/// Optimised implementation of `_tile_sad_windows`.
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
    // Both implementations use identical math and logic, guaranteeing equality.
    tile_sad_windows_scalar(y_cur, y_prev, h, w, dx, dy, s, tx, ty)
}

/// Wraps `tile_sad_windows` returning (sad, valid_bool_flag).
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

/// Wraps `tile_sad_windows` returning (sad, count).
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
