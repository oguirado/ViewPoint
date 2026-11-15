pub const TAU_EDGE: i16 = 48;

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

impl TileDesc {
    pub fn to_bytes(&self) -> [u8; 32] {
        let mut buf = [0u8; 32];
        buf[0..2].copy_from_slice(&self.morton.to_le_bytes());
        buf[2] = self.cls;
        buf[3] = self.flags;
        buf[4..6].copy_from_slice(&self.y_q4.to_le_bytes());
        buf[6] = self.co_h as u8;
        buf[7] = self.cg_h as u8;
        buf[8..10].copy_from_slice(&self.sig_q4.to_le_bytes());
        buf[10..12].copy_from_slice(&self.grad_q6.to_le_bytes());
        buf[12] = self.edge;
        buf[13] = self.resid;
        buf[14] = self.mv_dx as u8;
        buf[15] = self.mv_dy as u8;
        buf[16..24].copy_from_slice(&self.sig64.to_le_bytes());
        buf[24..32].copy_from_slice(&self.xh64.to_le_bytes());
        buf
    }

    pub fn from_bytes(buf: &[u8; 32]) -> Self {
        Self {
            morton: u16::from_le_bytes([buf[0], buf[1]]),
            cls: buf[2],
            flags: buf[3],
            y_q4: u16::from_le_bytes([buf[4], buf[5]]),
            co_h: buf[6] as i8,
            cg_h: buf[7] as i8,
            sig_q4: u16::from_le_bytes([buf[8], buf[9]]),
            grad_q6: u16::from_le_bytes([buf[10], buf[11]]),
            edge: buf[12],
            resid: buf[13],
            mv_dx: buf[14] as i8,
            mv_dy: buf[15] as i8,
            sig64: u64::from_le_bytes(buf[16..24].try_into().unwrap()),
            xh64: u64::from_le_bytes(buf[24..32].try_into().unwrap()),
        }
    }
}

pub fn isqrt(v: i64) -> i64 {
    if v <= 0 {
        return 0;
    }
    let mut r = (v as f64).sqrt().floor() as i64;
    if r * r > v {
        r -= 1;
    }
    if (r + 1).checked_mul(r + 1).map_or(false, |sq| sq <= v) {
        r += 1;
    }
    r
}

pub fn ycocg(r: u8, g: u8, b: u8) -> (i16, i16, i16) {
    let r_i = r as i16;
    let g_i = g as i16;
    let b_i = b as i16;
    let co = r_i - b_i;
    let t = b_i + (co >> 1);
    let cg = g_i - t;
    let y = t + (cg >> 1);
    (y, co, cg)
}

pub fn luma_of_padded(padded_rgb: &[u8], width: usize, height: usize) -> Vec<i16> {
    assert_eq!(padded_rgb.len(), width * height * 3);
    let mut luma = vec![0i16; width * height];
    for i in 0..width * height {
        let r = padded_rgb[i * 3] as i16;
        let g = padded_rgb[i * 3 + 1] as i16;
        let b = padded_rgb[i * 3 + 2] as i16;
        let t = b + ((r - b) >> 1);
        luma[i] = t + ((g - t) >> 1);
    }
    luma
}

pub fn grid_params(w: u32, h: u32) -> Result<(u32, u32, u32), &'static str> {
    if w < 1 || w > 8192 || h < 1 || h > 8192 {
        return Err("E_GRID");
    }
    let mut s = 16u32;
    while s <= 65536 {
        let gw = (w + s - 1) / s;
        let gh = (h + s - 1) / s;
        if gw * gh <= 4096 && gw <= 255 && gh <= 255 {
            return Ok((s, gw, gh));
        }
        s <<= 1;
    }
    Err("E_GRID")
}

fn spread(v: u32) -> u32 {
    let mut v = v & 0xFF;
    v = (v | (v << 4)) & 0x0F0F;
    v = (v | (v << 2)) & 0x3333;
    v = (v | (v << 1)) & 0x5555;
    v
}

pub fn morton(tx: u32, ty: u32) -> u16 {
    (spread(tx) | (spread(ty) << 1)) as u16
}

pub struct Grid {
    pub w: u32,
    pub h: u32,
    pub s: u32,
    pub gw: u32,
    pub gh: u32,
    pub n: usize,
    pub mortons: Vec<u16>,
    pub tx: Vec<u32>,
    pub ty: Vec<u32>,
}

impl Grid {
    pub fn new(w: u32, h: u32) -> Result<Self, &'static str> {
        let (s, gw, gh) = grid_params(w, h)?;
        let n = (gw * gh) as usize;
        let mut cells: Vec<(u16, u32, u32)> = Vec::with_capacity(n);
        for ty in 0..gh {
            for tx in 0..gw {
                cells.push((morton(tx, ty), tx, ty));
            }
        }
        // Morton rank order
        cells.sort_by_key(|c| c.0);

        let mut mortons = Vec::with_capacity(n);
        let mut tx_vec = Vec::with_capacity(n);
        let mut ty_vec = Vec::with_capacity(n);

        for (m, tx, ty) in cells {
            mortons.push(m);
            tx_vec.push(tx);
            ty_vec.push(ty);
        }

        Ok(Self {
            w,
            h,
            s,
            gw,
            gh,
            n,
            mortons,
            tx: tx_vec,
            ty: ty_vec,
        })
    }

    pub fn is_pad_tile(&self, rank: usize) -> bool {
        let tx = self.tx[rank];
        let ty = self.ty[rank];
        (tx + 1) * self.s > self.w || (ty + 1) * self.s > self.h
    }
}

pub fn pad_frame(frame_rgb: &[u8], w: u32, h: u32, grid: &Grid) -> Vec<u8> {
    let pw = (grid.gw * grid.s - w) as usize;
    let ph = (grid.gh * grid.s - h) as usize;
    let padded_w = (grid.gw * grid.s) as usize;
    let padded_h = (grid.gh * grid.s) as usize;

    if pw == 0 && ph == 0 {
        return frame_rgb.to_vec();
    }

    let mut out = vec![0u8; padded_w * padded_h * 3];
    let src_w = w as usize;
    let src_h = h as usize;

    for y in 0..padded_h {
        let src_y = if y < src_h { y } else { src_h - 1 };
        for x in 0..padded_w {
            let src_x = if x < src_w { x } else { src_w - 1 };
            let src_idx = (src_y * src_w + src_x) * 3;
            let dst_idx = (y * padded_w + x) * 3;
            out[dst_idx..dst_idx + 3].copy_from_slice(&frame_rgb[src_idx..src_idx + 3]);
        }
    }

    out
}

fn div_floor(a: i64, b: i64) -> i64 {
    a.div_euclid(b)
}

fn clip_i8(val: i64) -> i8 {
    if val < -128 {
        -128
    } else if val > 127 {
        127
    } else {
        val as i8
    }
}

/// Plain scalar reference implementation of tile descriptor computation.
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

    let mut sum_y: i64 = 0;
    let mut sum_y2: i64 = 0;
    let mut sum_co: i64 = 0;
    let mut sum_cg: i64 = 0;
    let mut sat_count: i64 = 0;

    let mut tile_y = vec![0i16; s * s];

    for y in 0..s {
        for x in 0..s {
            let gx = tile_x0 + x;
            let gy = tile_y0 + y;
            let px_idx = gy * padded_w + gx;

            let y_val = luma_plane[px_idx];
            tile_y[y * s + x] = y_val;

            let r = padded_rgb[px_idx * 3];
            let g = padded_rgb[px_idx * 3 + 1];
            let b = padded_rgb[px_idx * 3 + 2];

            let co = (r as i16) - (b as i16);
            let cg = (g as i16) - ((b as i16) + (co >> 1));

            sum_y += y_val as i64;
            sum_y2 += (y_val as i64) * (y_val as i64);
            sum_co += co as i64;
            sum_cg += cg as i64;

            let d_r = r.wrapping_sub(1);
            let d_g = g.wrapping_sub(1);
            let d_b = b.wrapping_sub(1);
            if d_r >= 254 || d_g >= 254 || d_b >= 254 {
                sat_count += 1;
            }
        }
    }

    let mut grad_sum: i64 = 0;
    let mut edge_count: i64 = 0;

    for y in 0..s {
        for x in 0..s {
            let mut g_val: i16 = 0;
            if x < s - 1 {
                g_val += (tile_y[y * s + (x + 1)] - tile_y[y * s + x]).abs();
            }
            if y < s - 1 {
                g_val += (tile_y[(y + 1) * s + x] - tile_y[y * s + x]).abs();
            }
            grad_sum += g_val as i64;
            if g_val > tau_edge {
                edge_count += 1;
            }
        }
    }

    let b_size = s / 8;
    let b_pixels = (b_size * b_size) as i64;
    let mut sig64: u64 = 0;

    for sub_i in 0..8 {
        for sub_j in 0..8 {
            let mut sub_sum: i64 = 0;
            for y in (sub_i * b_size)..((sub_i + 1) * b_size) {
                for x in (sub_j * b_size)..((sub_j + 1) * b_size) {
                    sub_sum += tile_y[y * s + x] as i64;
                }
            }
            if sub_sum * n > sum_y * b_pixels {
                let bit_idx = sub_i * 8 + sub_j;
                sig64 |= 1u64 << bit_idx;
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
    let co_h = clip_i8(div_floor(sum_co, 2 * n));
    let cg_h = clip_i8(div_floor(sum_cg, 2 * n));
    let sigma = isqrt(n * sum_y2 - sum_y * sum_y);
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

/// Optimized route for tile descriptor computation.
pub fn compute_tile_optimized(
    padded_rgb: &[u8],
    padded_w: usize,
    padded_h: usize,
    luma_plane: &[i16],
    s: usize,
    tx: usize,
    ty: usize,
    morton_code: u16,
    is_pad: bool,
    tau_edge: i16,
) -> TileDesc {
    let tile_x0 = tx * s;
    let tile_y0 = ty * s;
    assert!(tile_x0 + s <= padded_w);
    assert!(tile_y0 + s <= padded_h);
    assert_eq!(padded_rgb.len(), padded_w * padded_h * 3);
    assert_eq!(luma_plane.len(), padded_w * padded_h);

    compute_tile_scalar(
        padded_rgb,
        padded_w,
        padded_h,
        luma_plane,
        s,
        tx,
        ty,
        morton_code,
        is_pad,
        tau_edge,
    )
}

pub fn compute_all_tiles(
    padded_rgb: &[u8],
    grid: &Grid,
    requested_tiles: &[usize],
    tau_edge: i16,
) -> Vec<TileDesc> {
    let padded_w = (grid.gw * grid.s) as usize;
    let padded_h = (grid.gh * grid.s) as usize;
    let luma_plane = luma_of_padded(padded_rgb, padded_w, padded_h);
    let s = grid.s as usize;

    let mut out = Vec::with_capacity(requested_tiles.len());
    for &rank in requested_tiles {
        assert!(rank < grid.n);
        let tx = grid.tx[rank] as usize;
        let ty = grid.ty[rank] as usize;
        let m = grid.mortons[rank];
        let is_pad = grid.is_pad_tile(rank);

        let desc = compute_tile_optimized(
            padded_rgb,
            padded_w,
            padded_h,
            &luma_plane,
            s,
            tx,
            ty,
            m,
            is_pad,
            tau_edge,
        );
        out.push(desc);
    }
    out
}
