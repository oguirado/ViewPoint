use crate::{Result, RingError};
pub const MAGIC: &[u8; 8] = b"VQARING1";
pub const CRC_VALID: u32 = 1;
pub const MEMFD: u32 = 2;
pub const HEAD: usize = 40;
pub const NOTIFY: usize = 48;
pub const SHUTDOWN: usize = 52;
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u32)]
pub enum Format {
    Rgb24 = 0,
    Bgrx32 = 1,
}
impl TryFrom<u32> for Format {
    type Error = RingError;
    fn try_from(v: u32) -> Result<Self> {
        match v {
            0 => Ok(Self::Rgb24),
            1 => Ok(Self::Bgrx32),
            _ => Err(RingError::Format),
        }
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Geometry {
    pub slots: u32,
    pub width: u32,
    pub height: u32,
    pub fmt: Format,
    pub slot_bytes: usize,
    pub data_off: usize,
    pub ring_bytes: usize,
}
impl Geometry {
    pub fn new(slots: u32, width: u32, height: u32, fmt: Format) -> Result<Self> {
        if slots == 0 || width == 0 || height == 0 {
            return Err(RingError::Size);
        }
        let slot_bytes = (width as usize)
            .checked_mul(height as usize)
            .and_then(|x| x.checked_mul(if fmt == Format::Rgb24 { 3 } else { 4 }))
            .ok_or(RingError::Size)?;
        let data_off = (slots as usize)
            .checked_mul(64)
            .and_then(|x| x.checked_add(64 + 4095))
            .map(|x| x & !4095)
            .ok_or(RingError::Size)?;
        let ring_bytes = slot_bytes
            .checked_mul(slots as usize)
            .and_then(|x| x.checked_add(data_off))
            .filter(|&x| x <= isize::MAX as usize)
            .ok_or(RingError::Size)?;
        Ok(Self {
            slots,
            width,
            height,
            fmt,
            slot_bytes,
            data_off,
            ring_bytes,
        })
    }
    pub fn slot(&self, r: u64) -> usize {
        64 + 64 * (r % self.slots as u64) as usize
    }
    pub fn payload(&self, r: u64) -> usize {
        self.data_off + self.slot_bytes * (r % self.slots as u64) as usize
    }
    pub fn header(&self, crc: bool) -> [u8; 64] {
        let mut h = [0; 64];
        h[..8].copy_from_slice(MAGIC);
        for (off, v) in [
            (8, 3),
            (12, self.slots),
            (16, self.width),
            (20, self.height),
            (56, self.fmt as u32),
            (60, MEMFD | if crc { CRC_VALID } else { 0 }),
        ] {
            h[off..off + 4].copy_from_slice(&v.to_le_bytes());
        }
        h[24..32].copy_from_slice(&(self.slot_bytes as u64).to_le_bytes());
        h[32..40].copy_from_slice(&(self.data_off as u64).to_le_bytes());
        h
    }
    pub fn parse(h: &[u8]) -> Result<(Self, u32, u32)> {
        if h.len() < 64 || &h[..8] != MAGIC {
            return Err(RingError::Format);
        }
        let u32at = |o| u32::from_le_bytes(h[o..o + 4].try_into().unwrap());
        let u64at = |o| u64::from_le_bytes(h[o..o + 8].try_into().unwrap());
        let version = u32at(8);
        if version != 2 && version != 3 {
            return Err(RingError::Format);
        }
        let flags = if version == 2 { CRC_VALID } else { u32at(60) };
        if version == 3 && (flags & !3 != 0 || flags & MEMFD == 0 || u32at(52) > 1) {
            return Err(RingError::Format);
        }
        let g = Self::new(
            u32at(12),
            u32at(16),
            u32at(20),
            if version == 2 {
                Format::Rgb24
            } else {
                Format::try_from(u32at(56))?
            },
        )?;
        if u64at(24) != g.slot_bytes as u64 || u64at(32) != g.data_off as u64 {
            return Err(RingError::Size);
        }
        Ok((g, flags, version))
    }
}
pub fn crc32(bytes: &[u8]) -> u32 {
    let mut c = !0u32;
    for &b in bytes {
        c ^= b as u32;
        for _ in 0..8 {
            c = (c >> 1) ^ (0xedb88320u32 & 0u32.wrapping_sub(c & 1));
        }
    }
    !c
}
