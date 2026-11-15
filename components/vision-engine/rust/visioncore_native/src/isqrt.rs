//! Integer floor square root (`isqrt_u64`, `isqrt_i64`) and deterministic mutation hooks for parity testing.
use std::sync::atomic::{AtomicU8, Ordering};

pub const MUT_NONE: u8 = 0;
pub const MUT_SAD_PLUS_ONE: u8 = 1;
pub const MUT_SWAP_DX_DY: u8 = 2;
pub const MUT_DROP_LAST_ISLAND: u8 = 3;
pub const MUT_FLIP_DESC_BYTE_0: u8 = 4;
pub const MUT_SORT_REGIONS_BY_BBOX: u8 = 5;
pub const MUT_MERGE_DISTINCT_MOV_GROUPS: u8 = 6;
pub const MUT_BAND_OFF_BY_ONE: u8 = 7;

static ACTIVE_MUTANT: AtomicU8 = AtomicU8::new(MUT_NONE);

#[inline]
pub fn active_mutant() -> u8 {
    ACTIVE_MUTANT.load(Ordering::Relaxed)
}

#[inline]
pub fn set_active_mutant(code: u8) {
    ACTIVE_MUTANT.store(code, Ordering::Relaxed);
}

pub fn mutant_from_name(name: Option<&str>) -> Result<u8, &'static str> {
    match name {
        None | Some("") | Some("NONE") => Ok(MUT_NONE),
        Some("SAD_PLUS_ONE") => Ok(MUT_SAD_PLUS_ONE),
        Some("SWAP_DX_DY") => Ok(MUT_SWAP_DX_DY),
        Some("DROP_LAST_ISLAND") => Ok(MUT_DROP_LAST_ISLAND),
        Some("FLIP_DESC_BYTE_0") => Ok(MUT_FLIP_DESC_BYTE_0),
        Some("SORT_REGIONS_BY_BBOX") => Ok(MUT_SORT_REGIONS_BY_BBOX),
        Some("MERGE_DISTINCT_MOV_GROUPS") => Ok(MUT_MERGE_DISTINCT_MOV_GROUPS),
        Some("BAND_OFF_BY_ONE") | Some("OFF_BY_ONE_BAND") => Ok(MUT_BAND_OFF_BY_ONE),
        _ => Err("unknown mutant name"),
    }
}

pub fn set_mutant_by_name(name: Option<&str>) -> Result<(), &'static str> {
    let code = mutant_from_name(name)?;
    set_active_mutant(code);
    Ok(())
}

pub fn active_mutant_name() -> Option<&'static str> {
    match active_mutant() {
        MUT_NONE => None,
        MUT_SAD_PLUS_ONE => Some("SAD_PLUS_ONE"),
        MUT_SWAP_DX_DY => Some("SWAP_DX_DY"),
        MUT_DROP_LAST_ISLAND => Some("DROP_LAST_ISLAND"),
        MUT_FLIP_DESC_BYTE_0 => Some("FLIP_DESC_BYTE_0"),
        MUT_SORT_REGIONS_BY_BBOX => Some("SORT_REGIONS_BY_BBOX"),
        MUT_MERGE_DISTINCT_MOV_GROUPS => Some("MERGE_DISTINCT_MOV_GROUPS"),
        MUT_BAND_OFF_BY_ONE => Some("BAND_OFF_BY_ONE"),
        _ => None,
    }
}

/// Exact integer floor square root for u64.
#[inline]
pub fn isqrt_u64(v: u64) -> u64 {
    if v == 0 {
        return 0;
    }
    let mut r = (v as f64).sqrt().floor() as u64;
    if r.saturating_mul(r) > v {
        r -= 1;
    }
    if let Some(sq) = (r + 1).checked_mul(r + 1) {
        if sq <= v {
            r += 1;
        }
    }
    r
}

/// Exact integer floor square root for i64 (0 for v <= 0), bit-identical to visioncore.desc.isqrt on [0, 2^52).
#[inline]
pub fn isqrt_i64(v: i64) -> i64 {
    if v <= 0 {
        return 0;
    }
    isqrt_u64(v as u64) as i64
}

pub fn isqrt_slice(vals: &[i64], out: &mut [i64]) {
    for (dst, &v) in out.iter_mut().zip(vals.iter()) {
        *dst = isqrt_i64(v);
    }
}

#[cfg(test)]
pub static TEST_MUTEX: std::sync::Mutex<()> = std::sync::Mutex::new(());

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_isqrt_exact_boundaries() {
        assert_eq!(isqrt_i64(-5), 0);
        assert_eq!(isqrt_i64(0), 0);
        assert_eq!(isqrt_i64(1), 1);
        assert_eq!(isqrt_i64(2), 1);
        assert_eq!(isqrt_i64(3), 1);
        assert_eq!(isqrt_i64(4), 2);
        assert_eq!(isqrt_i64((1i64 << 52) - 1), (1i64 << 26) - 1);
        for k in [1i64, 2, 15, 16, 255, 1024, 65535, (1 << 25)] {
            assert_eq!(isqrt_i64(k * k - 1), k - 1);
            assert_eq!(isqrt_i64(k * k), k);
            assert_eq!(isqrt_i64(k * k + 1), k);
        }
    }
}
