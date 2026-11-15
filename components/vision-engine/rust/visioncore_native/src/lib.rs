//! visioncore_native: RT1 skeleton. Kernels land in RT2 (s0/s1/isqrt/par stubs); RT2 lanes never touch this file or Cargo.toml.
pub mod isqrt;
pub mod par;
pub mod py;
pub mod s0;
pub mod s1;

pub const ABI_VERSION: u32 = 1;

/// Mirror of visioncore.desc.TILE_DTYPE: (name, offset, size, kind) with kind u (unsigned) or i (signed), as numpy dtype.kind. Checked against Python by layout_check().
pub const TILE_FIELDS: [(&str, usize, usize, char); 14] = [
    ("morton", 0, 2, 'u'),
    ("cls", 2, 1, 'u'),
    ("flags", 3, 1, 'u'),
    ("y_q4", 4, 2, 'u'),
    ("co_h", 6, 1, 'i'),
    ("cg_h", 7, 1, 'i'),
    ("sig_q4", 8, 2, 'u'),
    ("grad_q6", 10, 2, 'u'),
    ("edge", 12, 1, 'u'),
    ("resid", 13, 1, 'u'),
    ("mv_dx", 14, 1, 'i'),
    ("mv_dy", 15, 1, 'i'),
    ("sig64", 16, 8, 'u'),
    ("xh64", 24, 8, 'u'),
];
pub const TILE_SIZE: usize = 32;
