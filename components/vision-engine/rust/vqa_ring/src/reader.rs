use crate::{
    layout::{self, Geometry},
    map,
    notify::{Deadline, WaitBackend},
    Result, RingError,
};
use std::{
    os::fd::{AsRawFd, OwnedFd},
    sync::{
        atomic::{fence, Ordering},
        Arc,
    },
    time::Duration,
};
#[derive(Debug)]
pub enum ReadResult {
    Frame(Frame),
    Lapped,
    Absent,
    Torn,
}
#[derive(Debug)]
pub struct Frame {
    pub render_index: u64,
    pub bytes: Vec<u8>,
    pub crc32: u32,
    seq: u64,
    slot: usize,
    map: Arc<map::Mapping>,
}
impl Frame {
    pub fn valid(&self) -> bool {
        self.map.u64(self.slot).load(Ordering::Acquire) == self.seq
    }
}
impl std::fmt::Debug for map::Mapping {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Mapping")
    }
}
pub struct Reader {
    _fd: OwnedFd,
    pub(crate) map: Arc<map::Mapping>,
    pub(crate) geometry: Geometry,
    pub flags: u32,
    pub version: u32,
    pub last: Option<u64>,
    pub overrun: Vec<u64>,
    pub torn: Vec<u64>,
    pub gaps: Vec<(u64, u64)>,
}
impl Reader {
    /// Validate seals and size before mapping. Handover additionally authenticates peer.
    pub fn from_fd(fd: OwnedFd, geometry: Geometry) -> Result<Self> {
        map::check_seals(fd.as_raw_fd())?;
        if map::stat(fd.as_raw_fd())?.st_size != geometry.ring_bytes as i64 {
            return Err(RingError::Size);
        }
        if Geometry::new(
            geometry.slots,
            geometry.width,
            geometry.height,
            geometry.fmt,
        )? != geometry
        {
            return Err(RingError::Size);
        }
        let m = Arc::new(map::Mapping::new(
            fd.as_raw_fd(),
            geometry.ring_bytes,
            false,
        )?);
        let (g, flags, version) = Geometry::parse(&m.header())?;
        if g != geometry {
            return Err(RingError::Size);
        }
        Ok(Self {
            _fd: fd,
            map: m,
            geometry,
            flags,
            version,
            last: None,
            overrun: vec![],
            torn: vec![],
            gaps: vec![],
        })
    }
    pub fn fd(&self) -> std::os::fd::BorrowedFd<'_> {
        std::os::fd::AsFd::as_fd(&self._fd)
    }
    pub fn geometry(&self) -> Geometry {
        self.geometry
    }
    pub fn head(&self) -> u64 {
        self.map.u64(layout::HEAD).load(Ordering::Acquire)
    }
    pub fn shutdown(&self) -> bool {
        self.map.u32(layout::SHUTDOWN).load(Ordering::Acquire) != 0
    }
    pub fn try_read(&self, r: u64, verify: bool) -> ReadResult {
        let _h = self.head();
        let slot = self.geometry.slot(r);
        for _ in 0..8 {
            let Some((s1, ri)) = self.stable_index(r) else {
                continue;
            };
            if s1 == 0 {
                return ReadResult::Absent;
            }
            if ri != r {
                return if ri > r {
                    ReadResult::Lapped
                } else {
                    ReadResult::Absent
                };
            }
            let bytes = self
                .map
                .load(self.geometry.payload(r), self.geometry.slot_bytes);
            let expected = self.map.u32(slot + 16).load(Ordering::Relaxed);
            fence(Ordering::Acquire);
            let s2 = self.map.u64(slot).load(Ordering::Relaxed);
            if s1 != s2 {
                continue;
            }
            let crc32 = if verify && self.flags & layout::CRC_VALID != 0 {
                layout::crc32(&bytes)
            } else {
                expected
            };
            if verify && self.flags & layout::CRC_VALID != 0 && crc32 != expected {
                continue;
            }
            return ReadResult::Frame(Frame {
                render_index: r,
                bytes,
                crc32,
                seq: s1,
                slot,
                map: self.map.clone(),
            });
        }
        ReadResult::Torn
    }
    // Shared by the reader and the aarch64 index/sequence coherence oracle.
    pub(crate) fn stable_index(&self, r: u64) -> Option<(u64, u64)> {
        let slot = self.geometry.slot(r);
        let s1 = self.map.u64(slot).load(Ordering::Acquire);
        if !cfg!(all(test, feature = "mut-parity")) && s1 & 1 != 0 {
            return None;
        }
        let ri = self.map.u64(slot + 8).load(Ordering::Relaxed);
        if ri != r {
            if index_recheck(self.map.u64(slot)) != s1 {
                return None;
            }
        }
        Some((s1, ri))
    }
    /// Acquire one explicit index and update v2-compatible drop/gap history.
    pub fn acquire(&mut self, r: u64, verify: bool) -> ReadResult {
        let result = self.try_read(r, verify);
        match &result {
            ReadResult::Frame(_) => {
                let next = self.last.map_or(0, |v| v + 1);
                if r > next {
                    self.gaps.push((next, r - 1));
                }
                self.last = Some(self.last.map_or(r, |v| v.max(r)));
            }
            ReadResult::Lapped => self.overrun.push(r),
            ReadResult::Torn => self.torn.push(r),
            ReadResult::Absent => {}
        }
        result
    }
    pub fn poll(&mut self, latest: bool, verify: bool) -> Vec<Frame> {
        if self.version == 2 {
            return self.poll_v2(latest, verify);
        }
        let h = self.head();
        let mut r = self.last.map_or(0, |v| v + 1);
        if h <= r {
            return vec![];
        }
        if latest && h - 1 > r {
            self.gaps.push((r, h - 2));
            r = h - 1;
        }
        let mut out = vec![];
        // Bound work by ring capacity even if an untrusted writer jumps head.
        if h - r > self.geometry.slots as u64 {
            let end = h - self.geometry.slots as u64;
            self.gaps.push((r, end - 1));
            r = end;
        }
        while r < h {
            match self.try_read(r, verify) {
                ReadResult::Frame(f) => out.push(f),
                ReadResult::Lapped => self.overrun.push(r),
                ReadResult::Absent => self.gaps.push((r, r)),
                ReadResult::Torn => self.torn.push(r),
            }
            self.last = Some(r);
            r += 1;
        }
        out
    }
    // Python rtring.RingReader.poll: one delivery, identical history accounting.
    fn poll_v2(&mut self, latest: bool, verify: bool) -> Vec<Frame> {
        let h = self.head();
        let next = self.last.map_or(0, |v| v + 1);
        if h <= next { return vec![]; }
        if latest {
            return match self.acquire(h - 1, verify) {
                ReadResult::Frame(f) => vec![f],
                _ => vec![],
            };
        }
        let lo = next.max(h.saturating_sub(self.geometry.slots as u64));
        if self.last.is_some() && lo > next {
            self.overrun.extend(next..lo);
            self.gaps.push((next, lo - 1));
        }
        for r in lo..h {
            match self.try_read(r, verify) {
                ReadResult::Frame(f) => {
                    self.last = Some(r);
                    return vec![f];
                }
                ReadResult::Lapped => self.overrun.push(r),
                ReadResult::Torn => self.torn.push(r),
                ReadResult::Absent => self.gaps.push((r,r)),
            }
            self.last = Some(r);
        }
        vec![]
    }
    pub fn wait(&self, timeout: Duration) -> Result<bool> {
        self.wait_with(timeout, WaitBackend::Auto)
    }
    pub fn wait_with(&self, timeout: Duration, backend: WaitBackend) -> Result<bool> {
        if self.version == 2 {
            return Err(RingError::Format);
        }
        let deadline = Deadline::after(timeout)?;
        let last = self.map.u32(layout::NOTIFY).load(Ordering::Acquire);
        let head = self.head();
        let consumed = self.last.map_or(0, |v| v + 1);
        if head > consumed {
            return Ok(true);
        }
        for _ in 0..if backend == WaitBackend::Auto { 64 } else { 0 } {
            if deadline.remaining()?.is_none() {
                return Ok(false);
            }
            if self.shutdown() {
                return Ok(false);
            }
            if self.map.u32(layout::NOTIFY).load(Ordering::Acquire) != last {
                return Ok(true);
            }
            std::thread::yield_now();
        }
        let mut fallback = backend == WaitBackend::Fallback;
        loop {
            if self.shutdown() {
                return Ok(false);
            }
            if self.map.u32(layout::NOTIFY).load(Ordering::Acquire) != last {
                return Ok(true);
            }
            let Some(relative) = deadline.remaining()? else {
                return Ok(false);
            };
            match map::wait(
                self.map.u32(layout::NOTIFY),
                self.map.u32(layout::SHUTDOWN),
                last,
                &deadline.absolute(),
                &relative,
                fallback,
            ) {
                Ok(()) => {}
                Err(e) => match e.raw_os_error() {
                    Some(libc::ENOSYS) if !fallback && backend == WaitBackend::Auto => {
                        fallback = true
                    }
                    Some(libc::EINTR) | Some(libc::EAGAIN) | Some(libc::ETIMEDOUT) => {}
                    _ => return Err(e.into()),
                },
            }
        }
    }
}

// Kept out of line so the exact ri != r recheck path has an auditable release
// instruction boundary. The mutation feature is deliberately effective in release
// libraries too; it is a verification-only feature, never a production option.
#[inline(never)]
#[no_mangle]
pub(crate) fn vqa_ring_index_recheck(seq: &std::sync::atomic::AtomicU64) -> u64 {
    if !cfg!(feature = "mut-ri-fence") {
        fence(Ordering::Acquire);
    }
    seq.load(Ordering::Relaxed)
}
use vqa_ring_index_recheck as index_recheck;
