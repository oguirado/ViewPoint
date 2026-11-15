//! Sole unsafe boundary: kernel calls, mapping ownership, bounded atomic access.
use crate::{Result, RingError};
use std::{
    io,
    os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd},
    ptr::NonNull,
    sync::atomic::{AtomicU32, AtomicU64, AtomicU8, Ordering},
};
pub const SEALS: i32 =
    libc::F_SEAL_SHRINK | libc::F_SEAL_GROW | libc::F_SEAL_FUTURE_WRITE | libc::F_SEAL_SEAL;
pub(crate) fn cv(v: i32) -> Result<i32> {
    if v < 0 {
        Err(io::Error::last_os_error().into())
    } else {
        Ok(v)
    }
}
pub fn create(size: usize) -> Result<OwnedFd> {
    unsafe {
        let fd = OwnedFd::from_raw_fd(cv(libc::memfd_create(
            c"vqa_ring".as_ptr(),
            libc::MFD_CLOEXEC | libc::MFD_ALLOW_SEALING,
        ))?);
        cv(libc::ftruncate(
            fd.as_raw_fd(),
            size.try_into().map_err(|_| RingError::Size)?,
        ))?;
        cv(libc::fcntl(
            fd.as_raw_fd(),
            libc::F_ADD_SEALS,
            libc::F_SEAL_SHRINK | libc::F_SEAL_GROW,
        ))?;
        Ok(fd)
    }
}
pub fn seal(fd: RawFd) -> Result<()> {
    unsafe {
        cv(libc::fcntl(
            fd,
            libc::F_ADD_SEALS,
            libc::F_SEAL_FUTURE_WRITE | libc::F_SEAL_SEAL,
        ))?;
    }
    Ok(())
}
pub fn stat(fd: RawFd) -> Result<libc::stat> {
    unsafe {
        let mut s = std::mem::zeroed();
        cv(libc::fstat(fd, &mut s))?;
        Ok(s)
    }
}
pub fn check_seals(fd: RawFd) -> Result<()> {
    if cfg!(all(test, feature = "mut-sealchk")) {
        return Ok(());
    }
    unsafe {
        let s = libc::fcntl(fd, libc::F_GET_SEALS);
        if s < 0 {
            return Err(
                if io::Error::last_os_error().raw_os_error() == Some(libc::EINVAL) {
                    RingError::NotMemfd
                } else {
                    io::Error::last_os_error().into()
                },
            );
        }
        if s == libc::F_SEAL_SEAL {
            return Err(RingError::NotMemfd);
        }
        if s & SEALS != SEALS {
            return Err(RingError::Unsealed);
        }
        Ok(())
    }
}
pub struct Mapping {
    ptr: NonNull<u8>,
    len: usize,
}
// Mapping never exposes references to non-atomic shared storage. Mapping lifetime
// owns munmap; all offsets are range/alignment checked before atomic accesses.
unsafe impl Send for Mapping {}
unsafe impl Sync for Mapping {}
impl Mapping {
    pub fn new(fd: RawFd, len: usize, writable: bool) -> Result<Self> {
        unsafe {
            if len == 0 || len > isize::MAX as usize {
                return Err(RingError::Size);
            }
            let p = libc::mmap(
                std::ptr::null_mut(),
                len,
                libc::PROT_READ | if writable { libc::PROT_WRITE } else { 0 },
                libc::MAP_SHARED | libc::MAP_POPULATE,
                fd,
                0,
            );
            if p == libc::MAP_FAILED {
                return Err(io::Error::last_os_error().into());
            }
            Ok(Self {
                ptr: NonNull::new(p.cast()).unwrap(),
                len,
            })
        }
    }
    pub fn header(&self) -> [u8; 64] {
        let mut h = [0; 64];
        h[..40].copy_from_slice(&self.load(0, 40));
        h[40..48].copy_from_slice(&self.u64(40).load(Ordering::Acquire).to_le_bytes());
        h[48..52].copy_from_slice(&self.u32(48).load(Ordering::Acquire).to_le_bytes());
        h[52..56].copy_from_slice(&self.u32(52).load(Ordering::Acquire).to_le_bytes());
        h[56..64].copy_from_slice(&self.load(56, 8));
        h
    }
    fn at<T>(&self, o: usize) -> *mut T {
        assert!(o <= self.len && std::mem::size_of::<T>() <= self.len - o);
        assert_eq!(o % std::mem::align_of::<T>(), 0);
        unsafe { self.ptr.as_ptr().add(o).cast() }
    }
    pub fn u64(&self, o: usize) -> &AtomicU64 {
        unsafe { &*self.at(o) }
    }
    pub fn u32(&self, o: usize) -> &AtomicU32 {
        unsafe { &*self.at(o) }
    }
    pub fn load(&self, o: usize, n: usize) -> Vec<u8> {
        let mut bytes = vec![0; n];
        self.load_into(o, &mut bytes);
        bytes
    }
    pub fn load_into(&self, o: usize, bytes: &mut [u8]) {
        for (i, byte) in bytes.iter_mut().enumerate() {
            *byte = unsafe { (&*self.at::<AtomicU8>(o + i)).load(Ordering::Relaxed) };
        }
    }
    pub fn store(&self, o: usize, b: &[u8]) {
        for (i, &v) in b.iter().enumerate() {
            unsafe { (&*self.at::<AtomicU8>(o + i)).store(v, Ordering::Relaxed) }
        }
    }
}
impl Drop for Mapping {
    fn drop(&mut self) {
        unsafe {
            libc::munmap(self.ptr.as_ptr().cast(), self.len);
        }
    }
}

pub fn monotonic() -> Result<libc::timespec> {
    unsafe {
        let mut t = std::mem::zeroed();
        cv(libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut t))?;
        Ok(t)
    }
}
pub fn wake(word: &AtomicU32) -> Result<usize> {
    #[cfg(test)]
    hot_loop_audit::record(false);
    unsafe {
        let op = libc::FUTEX_WAKE
            | if cfg!(all(test, feature = "mut-private")) {
                libc::FUTEX_PRIVATE_FLAG
            } else {
                0
            };
        let rc = libc::syscall(libc::SYS_futex, word as *const AtomicU32, op, i32::MAX);
        if rc < 0 {
            Err(io::Error::last_os_error().into())
        } else {
            Ok(rc as usize)
        }
    }
}
#[repr(C)]
struct Waitv {
    val: u64,
    uaddr: u64,
    flags: u32,
    reserved: u32,
}
pub fn wait(
    notify: &AtomicU32,
    shutdown: &AtomicU32,
    last: u32,
    absolute: &libc::timespec,
    relative: &libc::timespec,
    fallback: bool,
) -> io::Result<()> {
    #[cfg(test)]
    hot_loop_audit::record(false);
    unsafe {
        let private = if cfg!(all(test, feature = "mut-private")) {
            libc::FUTEX_PRIVATE_FLAG as u32
        } else {
            0
        };
        let list = [
            Waitv {
                val: last as u64,
                uaddr: notify as *const _ as u64,
                flags: 2 | private,
                reserved: 0,
            },
            Waitv {
                val: 0,
                uaddr: shutdown as *const _ as u64,
                flags: 2 | private,
                reserved: 0,
            },
        ];
        let rc = if fallback {
            libc::syscall(
                libc::SYS_futex,
                notify as *const _,
                libc::FUTEX_WAIT | private as i32,
                last,
                relative,
            )
        } else {
            libc::syscall(
                libc::SYS_futex_waitv,
                list.as_ptr(),
                2u32,
                0u32,
                absolute,
                libc::CLOCK_MONOTONIC,
            )
        };
        if rc < 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(())
        }
    }
}

pub fn peer(fd: RawFd) -> Result<libc::ucred> {
    unsafe {
        let mut c = std::mem::zeroed();
        let mut n = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
        cv(libc::getsockopt(
            fd,
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            (&mut c as *mut libc::ucred).cast(),
            &mut n,
        ))?;
        if n as usize != std::mem::size_of::<libc::ucred>() {
            return Err(RingError::Peer);
        }
        Ok(c)
    }
}
pub fn check_socket(fd: RawFd) -> Result<()> {
    unsafe {
        let mut t = 0i32;
        let mut n = 4;
        cv(libc::getsockopt(
            fd,
            libc::SOL_SOCKET,
            libc::SO_TYPE,
            (&mut t as *mut i32).cast(),
            &mut n,
        ))?;
        if t != libc::SOCK_SEQPACKET {
            return Err(RingError::Format);
        }
        let mut pathname = false;
        for peer in [false, true] {
            let mut addr: libc::sockaddr_un = std::mem::zeroed();
            let mut len = std::mem::size_of_val(&addr) as libc::socklen_t;
            let rc = if peer {
                libc::getpeername(fd, (&mut addr as *mut libc::sockaddr_un).cast(), &mut len)
            } else {
                libc::getsockname(fd, (&mut addr as *mut libc::sockaddr_un).cast(), &mut len)
            };
            cv(rc)?;
            if addr.sun_family != libc::AF_UNIX as libc::sa_family_t {
                return Err(RingError::Format);
            }
            pathname |= len > 3 && addr.sun_path[0] != 0;
        }
        if !pathname {
            return Err(RingError::Peer);
        }
        Ok(())
    }
}
// Control storage is machine-word aligned; every received fd is immediately owned,
// including descriptors in rejected/truncated messages.
pub fn send(fd: RawFd, bytes: &[u8], rights: &[RawFd]) -> Result<()> {
    unsafe {
        let space = libc::CMSG_SPACE(std::mem::size_of_val(rights) as u32) as usize;
        let mut control = vec![0usize; space.div_ceil(std::mem::size_of::<usize>())];
        let mut iov = libc::iovec {
            iov_base: bytes.as_ptr().cast_mut().cast(),
            iov_len: bytes.len(),
        };
        let mut msg: libc::msghdr = std::mem::zeroed();
        msg.msg_iov = &mut iov;
        msg.msg_iovlen = 1;
        msg.msg_control = control.as_mut_ptr().cast();
        msg.msg_controllen = space;
        let c = libc::CMSG_FIRSTHDR(&msg);
        (*c).cmsg_level = libc::SOL_SOCKET;
        (*c).cmsg_type = libc::SCM_RIGHTS;
        (*c).cmsg_len = libc::CMSG_LEN(std::mem::size_of_val(rights) as u32) as usize;
        std::ptr::copy_nonoverlapping(rights.as_ptr(), libc::CMSG_DATA(c).cast(), rights.len());
        loop {
            let n = libc::sendmsg(fd, &msg, libc::MSG_NOSIGNAL);
            if n == bytes.len() as isize {
                return Ok(());
            }
            if n < 0 && io::Error::last_os_error().raw_os_error() == Some(libc::EINTR) {
                continue;
            }
            return Err(if n < 0 {
                io::Error::last_os_error().into()
            } else {
                RingError::Ctrunc
            });
        }
    }
}
pub fn recv(fd: RawFd) -> Result<([u8; 64], OwnedFd)> {
    unsafe {
        let mut bytes = [0; 64];
        let mut control = [0usize; 128];
        let mut iov = libc::iovec {
            iov_base: bytes.as_mut_ptr().cast(),
            iov_len: 64,
        };
        let mut msg: libc::msghdr = std::mem::zeroed();
        msg.msg_iov = &mut iov;
        msg.msg_iovlen = 1;
        msg.msg_control = control.as_mut_ptr().cast();
        msg.msg_controllen = std::mem::size_of_val(&control);
        let n = loop {
            let n = libc::recvmsg(fd, &mut msg, libc::MSG_CMSG_CLOEXEC);
            if n < 0 && io::Error::last_os_error().raw_os_error() == Some(libc::EINTR) {
                continue;
            }
            break n;
        };
        if n < 0 {
            return Err(io::Error::last_os_error().into());
        }
        let mut rights = Vec::new();
        let mut c = libc::CMSG_FIRSTHDR(&msg);
        let mut bad = false;
        while !c.is_null() {
            if (*c).cmsg_level == libc::SOL_SOCKET && (*c).cmsg_type == libc::SCM_RIGHTS {
                let len = (*c).cmsg_len.saturating_sub(libc::CMSG_LEN(0) as usize);
                if len % 4 != 0 {
                    bad = true
                }
                for i in 0..len / 4 {
                    rights.push(OwnedFd::from_raw_fd(std::ptr::read_unaligned(
                        libc::CMSG_DATA(c).cast::<i32>().add(i),
                    )));
                }
            } else {
                bad = true
            }
            c = libc::CMSG_NXTHDR(&msg, c);
        }
        if bad
            || n != 64
            || msg.msg_flags & (libc::MSG_CTRUNC | libc::MSG_TRUNC) != 0
            || rights.len() != 1
        {
            return Err(RingError::Ctrunc);
        }
        Ok((bytes, rights.pop().unwrap()))
    }
}

#[cfg(test)]
mod tests {
    use crate::*;
    use crate::{
        handover::{Handshake, Policy},
        map,
        notify::WaitBackend,
    };
    use std::{
        os::fd::{AsFd, AsRawFd, FromRawFd, OwnedFd},
        sync::atomic::{AtomicU64, Ordering},
        time::{Duration, Instant},
    };
    fn geometry() -> Geometry {
        Geometry::new(2, 4, 4, Format::Rgb24).unwrap()
    }
    fn frame(r: u64, g: Geometry) -> Vec<u8> {
        (0..g.slot_bytes)
            .map(|i| (r.wrapping_mul(131) + (i as u64) * 7 + 19) as u8)
            .collect()
    }
    fn code<T>(r: Result<T>, want: &str) {
        match r {
            Err(e) => assert_eq!(e.code(), want),
            Ok(_) => panic!("expected {want}"),
        }
    }
    fn unsealed(size: usize, seals: i32) -> OwnedFd {
        unsafe {
            let fd = OwnedFd::from_raw_fd(libc::memfd_create(
                c"negative".as_ptr(),
                libc::MFD_CLOEXEC | libc::MFD_ALLOW_SEALING,
            ));
            assert!(fd.as_raw_fd() >= 0);
            assert_eq!(libc::ftruncate(fd.as_raw_fd(), size as i64), 0);
            let header = geometry().header(true);
            assert_eq!(
                libc::pwrite(fd.as_raw_fd(), header.as_ptr().cast(), 64, 0),
                64
            );
            assert_eq!(libc::fcntl(fd.as_raw_fd(), libc::F_ADD_SEALS, seals), 0);
            fd
        }
    }
    #[test]
    fn n1_n2_seals() {
        let g = geometry();
        code(
            Reader::from_fd(unsealed(g.ring_bytes, 0), g),
            "E_RING_UNSEALED",
        );
        for missing in [
            libc::F_SEAL_SHRINK,
            libc::F_SEAL_GROW,
            libc::F_SEAL_FUTURE_WRITE,
            libc::F_SEAL_SEAL,
        ] {
            code(
                Reader::from_fd(unsealed(g.ring_bytes, map::SEALS & !missing), g),
                "E_RING_UNSEALED",
            );
        }
    }
    #[test]
    fn n3_not_memfd() {
        // Anonymous O_TMPFILE on tmpfs, no pathname or frame bytes written to disk.
        let fd = unsafe {
            OwnedFd::from_raw_fd(libc::open(
                c"/dev/shm".as_ptr(),
                libc::O_TMPFILE | libc::O_RDWR | libc::O_CLOEXEC,
                0o600,
            ))
        };
        assert!(fd.as_raw_fd() >= 0);
        code(Reader::from_fd(fd, geometry()), "E_RING_NOTMEMFD");
    }
    #[test]
    fn n5_sizes() {
        let g = geometry();
        code(
            Reader::from_fd(unsealed(g.ring_bytes + 1, map::SEALS), g),
            "E_RING_SIZE",
        );
        let mut hand = Handshake {
            geometry: g,
            pid: std::process::id(),
            nonce: 0,
        }
        .encode();
        hand[8..16].copy_from_slice(&0u64.to_le_bytes());
        code(Handshake::decode(&hand), "E_RING_SIZE");
    }
    #[test]
    fn n7_received_writes_forbidden() {
        let w = Writer::create(geometry(), true).unwrap();
        let uid = unsafe { libc::getuid() };
        let (a, b) = connection();
        handover::send(a.as_fd(), &w, &[uid], 7).unwrap();
        let (received, _) = handover::recv(
            b.as_fd(),
            &Policy {
                allow_uids: &[uid],
                expected_nonce: Some(7),
            },
        )
        .unwrap();
        let fd = received.fd().as_raw_fd();
        unsafe {
            let p = libc::mmap(
                std::ptr::null_mut(),
                w.geometry.ring_bytes,
                libc::PROT_WRITE,
                libc::MAP_SHARED,
                fd,
                0,
            );
            assert_eq!(p, libc::MAP_FAILED);
            assert_eq!(
                std::io::Error::last_os_error().raw_os_error(),
                Some(libc::EPERM)
            );
            let b = [1u8];
            assert_eq!(libc::write(fd, b.as_ptr().cast(), 1), -1);
            assert_eq!(
                std::io::Error::last_os_error().raw_os_error(),
                Some(libc::EPERM)
            );
            assert_eq!(libc::ftruncate(fd, 1), -1);
        }
    }
    fn cpu() -> Duration {
        unsafe {
            let mut r: libc::rusage = std::mem::zeroed();
            assert_eq!(libc::getrusage(libc::RUSAGE_THREAD, &mut r), 0);
            Duration::from_micros(
                (r.ru_utime.tv_sec + r.ru_stime.tv_sec) as u64 * 1_000_000
                    + (r.ru_utime.tv_usec + r.ru_stime.tv_usec) as u64,
            )
        }
    }

    #[test]
    fn n9_deadline_idle_and_teardown() {
        for backend in [WaitBackend::Waitv, WaitBackend::Fallback] {
            let mut w = Writer::create(geometry(), false).unwrap();
            let r = w.reader().unwrap();
            let start = Instant::now();
            let c = cpu();
            assert!(!r.wait_with(Duration::from_millis(50), backend).unwrap());
            let elapsed = start.elapsed();
            assert!(
                elapsed >= Duration::from_millis(45) && elapsed <= Duration::from_millis(150),
                "idle {backend:?}: {elapsed:?}"
            );
            assert!(cpu() - c < Duration::from_millis(5));
            let t = std::thread::spawn(move || {
                assert!(!r.wait_with(Duration::from_secs(2), backend).unwrap());
                assert!(r.shutdown());
                Instant::now()
            });
            std::thread::sleep(Duration::from_millis(20));
            let closing = Instant::now();
            w.close().unwrap();
            assert!(
                t.join().unwrap().saturating_duration_since(closing)
                    < Duration::from_nanos(16_666_667)
            );
        }
    }
    // Real filesystem AF_UNIX connect/accept, never socketpair. Socket is a lane
    // scratch control object; all frame payloads remain in memfd.
    fn connection() -> (OwnedFd, OwnedFd) {
        unsafe {
            let socket = || {
                let v = libc::socket(libc::AF_UNIX, libc::SOCK_SEQPACKET | libc::SOCK_CLOEXEC, 0);
                assert!(v >= 0);
                OwnedFd::from_raw_fd(v)
            };
            let server = socket();
            let client = socket();
            let path = format!(
                "{}/../../build/room_R/handoff-{}-{}",
                env!("CARGO_MANIFEST_DIR"),
                std::process::id(),
                COUNTER.fetch_add(1, Ordering::Relaxed)
            );
            let mut addr: libc::sockaddr_un = std::mem::zeroed();
            addr.sun_family = libc::AF_UNIX as _;
            for (i, b) in path.bytes().enumerate() {
                addr.sun_path[i] = b as _;
            }
            assert_eq!(
                libc::bind(
                    server.as_raw_fd(),
                    (&addr as *const libc::sockaddr_un).cast(),
                    std::mem::size_of_val(&addr) as _
                ),
                0
            );
            assert_eq!(libc::listen(server.as_raw_fd(), 1), 0);
            assert_eq!(
                libc::connect(
                    client.as_raw_fd(),
                    (&addr as *const libc::sockaddr_un).cast(),
                    std::mem::size_of_val(&addr) as _
                ),
                0
            );
            let accepted = libc::accept4(
                server.as_raw_fd(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                libc::SOCK_CLOEXEC,
            );
            assert!(accepted >= 0);
            std::fs::remove_file(path).unwrap();
            (client, OwnedFd::from_raw_fd(accepted))
        }
    }
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    #[test]
    fn handover_and_n4_n6() {
        let uid = unsafe { libc::getuid() };
        let g = geometry();
        let mut w = Writer::create(g, true).unwrap();
        w.publish(0, &frame(0, g)).unwrap();
        let (a, b) = connection();
        handover::send(a.as_fd(), &w, &[uid], 42).unwrap();
        let (r, h) = handover::recv(
            b.as_fd(),
            &Policy {
                allow_uids: &[uid],
                expected_nonce: Some(42),
            },
        )
        .unwrap();
        assert_eq!(h.nonce, 42);
        assert!(matches!(r.try_read(0, true), ReadResult::Frame(_)));
        let (a, b) = connection();
        handover::send(a.as_fd(), &w, &[uid], 42).unwrap();
        code(
            handover::recv(
                b.as_fd(),
                &Policy {
                    allow_uids: &[],
                    expected_nonce: None,
                },
            ),
            "E_RING_PEER",
        );
        for count in [0, 2, 253] {
            let (a, b) = connection();
            let h = Handshake {
                geometry: g,
                pid: std::process::id(),
                nonce: 0,
            };
            if count == 0 {
                unsafe {
                    assert_eq!(
                        libc::send(a.as_raw_fd(), h.encode().as_ptr().cast(), 64, 0),
                        64
                    );
                }
            } else {
                map::send(a.as_raw_fd(), &h.encode(), &vec![w.fd.as_raw_fd(); count]).unwrap();
            }
            code(
                handover::recv(
                    b.as_fd(),
                    &Policy {
                        allow_uids: &[uid],
                        expected_nonce: None,
                    },
                ),
                "E_RING_CTRUNC",
            );
        }
    }
    #[test]
    fn shared_process_wake() {
        unsafe {
            let mut w = Writer::create(geometry(), false).unwrap();
            let reader = w.reader().unwrap();
            let pid = libc::fork();
            assert!(pid >= 0);
            if pid == 0 {
                let start = Instant::now();
                let ok = reader
                    .wait_with(Duration::from_millis(700), WaitBackend::Fallback)
                    .unwrap_or(false)
                    && start.elapsed() < Duration::from_millis(400);
                libc::_exit(if ok { 0 } else { 71 });
            }
            std::thread::sleep(Duration::from_millis(80));
            w.publish(0, &frame(0, geometry())).unwrap();
            let mut status = 0;
            assert_eq!(libc::waitpid(pid, &mut status, 0), pid);
            assert_eq!(status, 0, "cross-process futex did not wake");
        }
    }
    // Execution-only memory-order litmus. Sequence 2*(r+1) identifies the exact
    // generation of render_index, independently of payload CRC. No forced outcome.
    #[test]
    fn inherited_socketpair_rejected() {
        unsafe {
            let mut pair = [0; 2];
            assert_eq!(
                libc::socketpair(
                    libc::AF_UNIX,
                    libc::SOCK_SEQPACKET | libc::SOCK_CLOEXEC,
                    0,
                    pair.as_mut_ptr()
                ),
                0
            );
            let a = OwnedFd::from_raw_fd(pair[0]);
            let _b = OwnedFd::from_raw_fd(pair[1]);
            let w = Writer::create(geometry(), false).unwrap();
            code(
                handover::send(a.as_fd(), &w, &[libc::getuid()], 0),
                "E_RING_PEER",
            );
        }
    }
    #[test]
    fn rejects_close_received_descriptors() {
        if std::env::var_os("VQA_FD_CENSUS_CHILD").is_none() {
            let rc = std::process::Command::new("timeout")
                .arg("15")
                .arg(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "map::tests::rejects_close_received_descriptors",
                    "--test-threads=1",
                ])
                .env("VQA_FD_CENSUS_CHILD", "1")
                .output()
                .unwrap();
            assert!(rc.status.success(), "{:?}", rc);
            return;
        }
        let (a, b) = connection();
        let w = Writer::create(geometry(), false).unwrap();
        let h = Handshake {
            geometry: geometry(),
            pid: std::process::id(),
            nonce: 0,
        };
        // F_GETFD census contains no descriptor contents and never reopens /proc.
        let count = || unsafe {
            (0..4096)
                .filter(|&fd| libc::fcntl(fd, libc::F_GETFD) >= 0)
                .count()
        };
        let before = count();
        for _ in 0..64 {
            map::send(a.as_raw_fd(), &h.encode(), &[w.fd.as_raw_fd(); 2]).unwrap();
            code(
                handover::recv(
                    b.as_fd(),
                    &Policy {
                        allow_uids: &[],
                        expected_nonce: None,
                    },
                ),
                "E_RING_CTRUNC",
            );
        }
        assert_eq!(count(), before);
    }
}

#[cfg(test)]
pub(crate) fn test_pin(cpu: usize) {
    unsafe {
        let mut set: libc::cpu_set_t = std::mem::zeroed();
        libc::CPU_ZERO(&mut set);
        libc::CPU_SET(cpu, &mut set);
        assert_eq!(
            libc::sched_setaffinity(0, std::mem::size_of_val(&set), &set),
            0
        );
    }
}

// Thread-local counters leave unrelated parallel tests outside the measured span.
#[cfg(test)]
pub(crate) mod hot_loop_audit {
    use std::alloc::{GlobalAlloc, Layout, System};
    use std::cell::Cell;
    thread_local! {
        static COUNTS: Cell<(bool, u64, u64)> = const { Cell::new((false, 0, 0)) };
    }
    pub fn record(heap: bool) {
        let _ = COUNTS.try_with(|c| {
            let (active, allocations, futex) = c.get();
            if active { c.set((true, allocations + u64::from(heap), futex + u64::from(!heap))); }
        });
    }
    pub fn start() { COUNTS.with(|c| c.set((true,0,0))); }
    pub fn finish() -> (u64,u64) {
        COUNTS.with(|c| { let (_,heap,futex)=c.replace((false,0,0)); (heap,futex) })
    }
    struct AuditAllocator;
    #[global_allocator]
    static ALLOCATOR: AuditAllocator = AuditAllocator;
    unsafe impl GlobalAlloc for AuditAllocator {
        unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
            record(true);
            unsafe { System.alloc(layout) }
        }
        unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
            record(true);
            unsafe { System.alloc_zeroed(layout) }
        }
        unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
            record(true);
            unsafe { System.dealloc(ptr,layout) }
        }
        unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, size: usize) -> *mut u8 {
            record(true);
            unsafe { System.realloc(ptr,layout,size) }
        }
    }
}
