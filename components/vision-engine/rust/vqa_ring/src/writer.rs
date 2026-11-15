use crate::{
    layout::{self, Geometry},
    map,
    reader::Reader,
    Result, RingError,
};
use std::{
    os::fd::{AsRawFd, BorrowedFd, OwnedFd},
    sync::{
        atomic::{fence, Ordering},
        Arc,
    },
};
pub struct Writer {
    pub(crate) fd: OwnedFd,
    pub(crate) map: Arc<map::Mapping>,
    pub(crate) geometry: Geometry,
    pub(crate) crc: bool,
    last: Option<u64>,
    active: Option<(u64, u64, Option<u32>)>,
}
impl Writer {
    pub fn create(geometry: Geometry, crc: bool) -> Result<Self> {
        // Public geometry is rederived, so forged field values never reach mmap.
        if Geometry::new(
            geometry.slots,
            geometry.width,
            geometry.height,
            geometry.fmt,
        )? != geometry
        {
            return Err(RingError::Size);
        }
        let fd = map::create(geometry.ring_bytes)?;
        let map = Arc::new(map::Mapping::new(
            fd.as_raw_fd(),
            geometry.ring_bytes,
            true,
        )?);
        map.store(0, &geometry.header(crc));
        map::seal(fd.as_raw_fd())?;
        Ok(Self {
            fd,
            map,
            geometry,
            crc,
            last: None,
            active: None,
        })
    }
    pub fn geometry(&self) -> Geometry {
        self.geometry
    }
    pub fn fd(&self) -> BorrowedFd<'_> {
        self.fd.as_fd()
    }
    pub fn reader(&self) -> Result<Reader> {
        Reader::from_fd(self.fd.try_clone()?, self.geometry)
    }
    /// PRE: increasing render index, no unfinished write. Leaves odd on abandonment.
    pub fn begin(&mut self, r: u64) -> Result<()> {
        if self.active.is_some()
            || self.closed()
            || r == u64::MAX
            || self.last.is_some_and(|v| r <= v)
        {
            return Err(RingError::Format);
        }
        let o = self.geometry.slot(r);
        let s = self.map.u64(o).load(Ordering::Relaxed);
        if s & 1 != 0 || s > u64::MAX - 2 {
            return Err(RingError::Format);
        }
        self.map.u64(o).store(s + 1, Ordering::Release);
        if !cfg!(all(test, feature = "mut-fence")) {
            fence(Ordering::Release);
        }
        self.map.u64(o + 8).store(r, Ordering::Relaxed);
        self.active = Some((r, s, None));
        Ok(())
    }
    pub fn write(&mut self, frame: &[u8]) -> Result<()> {
        let (r, _, _) = self.active.ok_or(RingError::Format)?;
        if frame.len() != self.geometry.slot_bytes {
            return Err(RingError::Format);
        }
        self.map.store(self.geometry.payload(r), frame);
        let crc = if self.crc { layout::crc32(frame) } else { 0 };
        self.active.as_mut().unwrap().2 = Some(crc);
        Ok(())
    }
    pub fn commit(&mut self) -> Result<()> {
        self.commit_inner(true)
    }
    // Stress isolates memory publication from notification syscalls.
    #[cfg(test)]
    pub(crate) fn commit_unnotified(&mut self) -> Result<()> {
        self.commit_inner(false)
    }
    fn commit_inner(&mut self, notify: bool) -> Result<()> {
        if self.closed() {
            return Err(RingError::Format);
        }
        let (r, s, written) = self.active.ok_or(RingError::Format)?;
        let crc = written.ok_or(RingError::Format)?;
        self.active = None;
        let o = self.geometry.slot(r);
        if self.crc {
            self.map.u32(o + 16).store(crc, Ordering::Relaxed);
        }
        if cfg!(all(test, feature = "mut-headorder")) {
            self.map.u64(layout::HEAD).store(r + 1, Ordering::Release);
            std::thread::yield_now();
        }
        self.map.u64(o).store(s + 2, Ordering::Release);
        if !cfg!(all(test, feature = "mut-headorder")) {
            self.map.u64(layout::HEAD).store(r + 1, Ordering::Release);
        }
        self.last = Some(r);
        if notify {
            self.map.u32(layout::NOTIFY).fetch_add(1, Ordering::Release);
            map::wake(self.map.u32(layout::NOTIFY))?;
        }
        Ok(())
    }
    pub fn publish(&mut self, r: u64, frame: &[u8]) -> Result<()> {
        if frame.len() != self.geometry.slot_bytes {
            return Err(RingError::Format);
        }
        self.begin(r)?;
        self.write(frame)?;
        self.commit()
    }
    pub fn closed(&self) -> bool {
        self.map.u32(layout::SHUTDOWN).load(Ordering::Acquire) != 0
    }
    pub fn close(&mut self) -> Result<()> {
        if self.closed() {
            return Ok(());
        }
        self.map.u32(layout::SHUTDOWN).store(1, Ordering::Release);
        self.map.u32(layout::NOTIFY).fetch_add(1, Ordering::Release);
        map::wake(self.map.u32(layout::NOTIFY))?;
        map::wake(self.map.u32(layout::SHUTDOWN))?;
        Ok(())
    }
}
use std::os::fd::AsFd;
impl Drop for Writer {
    fn drop(&mut self) {
        let _ = self.close();
    }
}
