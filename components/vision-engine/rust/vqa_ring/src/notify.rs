use crate::{map, Result, RingError};
use std::time::Duration;
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WaitBackend {
    Auto,
    Waitv,
    Fallback,
}
/// One CLOCK_MONOTONIC absolute deadline, preserved over all retries.
#[derive(Clone, Copy, Debug)]
pub struct Deadline {
    sec: i64,
    nsec: i64,
}
impl Deadline {
    pub fn after(d: Duration) -> Result<Self> {
        let now = map::monotonic()?;
        let nanos = now.tv_nsec as u64 + d.subsec_nanos() as u64;
        let sec = now
            .tv_sec
            .checked_add(d.as_secs().try_into().map_err(|_| RingError::Size)?)
            .and_then(|v| v.checked_add((nanos / 1_000_000_000) as i64))
            .ok_or(RingError::Size)?;
        Ok(Self {
            sec,
            nsec: (nanos % 1_000_000_000) as i64,
        })
    }
    pub fn absolute(&self) -> libc::timespec {
        libc::timespec {
            tv_sec: self.sec,
            tv_nsec: self.nsec,
        }
    }
    pub fn remaining(&self) -> Result<Option<libc::timespec>> {
        let n = map::monotonic()?;
        let mut sec = self.sec - n.tv_sec;
        let mut nano = self.nsec - n.tv_nsec;
        if nano < 0 {
            sec -= 1;
            nano += 1_000_000_000
        }
        if sec < 0 || sec == 0 && nano == 0 {
            Ok(None)
        } else {
            Ok(Some(libc::timespec {
                tv_sec: sec,
                tv_nsec: nano,
            }))
        }
    }
}
