//! Adversarial integration tests for rust/vqa_ring handover::send, recv, verify_fd, and Reader::wait.
//!
//! Findings targeting open defects:
//! - SR1-07: verify_fd accepts a version 2 header on a version 3 handover.
//! - SR1-08: check_socket is bypassed by calling bind on one end of a socketpair.
//! - SR1-09: The default Auto wait backend spin path is untested.
//!
//! Rust has no expectedFailure feature: for a bug finding, we assert the correct behavior with an
//! assertion message starting with the finding ID (e.g., "SR1-07: ...") and mark the test function
//! `#[should_panic(expected = "SR1-07")]`. When the bug is subsequently fixed in the codebase, the test
//! will fail to panic and force its removal.
//!
//! STORAGE LAW:
//! Frames, luma planes and pixel buffers live only in RAM and are never written to disk; every fixture
//! is generated in RAM from fixed seeds and binary fixture files are never committed.
//!
//! VM TIMING LABEL:
//! Any timing measured in the VM environment is labeled as VM, never MEASURED; the host decides performance.

use std::{
    os::fd::{AsFd, AsRawFd, FromRawFd, OwnedFd, RawFd},
    sync::Mutex,
    time::{Duration, Instant},
};
use vqa_ring::{
    error::RingError,
    handover::{self, Handshake, Policy},
    layout::{Format, Geometry},
    writer::Writer,
};

static FD_LOCK: Mutex<()> = Mutex::new(());

fn count_open_fds() -> usize {
    unsafe {
        (0..4096)
            .filter(|&fd| libc::fcntl(fd, libc::F_GETFD) >= 0)
            .count()
    }
}

fn create_custom_memfd(header: &[u8; 64], size: usize, seals: i32) -> OwnedFd {
    unsafe {
        let fd = OwnedFd::from_raw_fd(libc::memfd_create(
            c"adv_memfd".as_ptr(),
            libc::MFD_CLOEXEC | libc::MFD_ALLOW_SEALING,
        ));
        assert!(fd.as_raw_fd() >= 0);
        assert_eq!(libc::ftruncate(fd.as_raw_fd(), size as i64), 0);
        assert_eq!(
            libc::pwrite(fd.as_raw_fd(), header.as_ptr().cast(), 64, 0),
            64
        );
        if seals != 0 {
            assert_eq!(libc::fcntl(fd.as_raw_fd(), libc::F_ADD_SEALS, seals), 0);
        }
        fd
    }
}

fn connection() -> (OwnedFd, OwnedFd) {
    static COUNTER: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    unsafe {
        let socket = || {
            let v = libc::socket(libc::AF_UNIX, libc::SOCK_SEQPACKET | libc::SOCK_CLOEXEC, 0);
            assert!(v >= 0);
            OwnedFd::from_raw_fd(v)
        };
        let server = socket();
        let client = socket();
        let path = format!(
            "/tmp/vqa-adv-handoff-{}-{}",
            std::process::id(),
            COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
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
        let _ = std::fs::remove_file(path);
        (client, OwnedFd::from_raw_fd(accepted))
    }
}

fn send_raw_cmsg(fd: RawFd, bytes: &[u8], rights: &[RawFd]) -> Result<(), std::io::Error> {
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
        let n = libc::sendmsg(fd, &msg, libc::MSG_NOSIGNAL);
        if n == bytes.len() as isize {
            Ok(())
        } else {
            Err(std::io::Error::last_os_error())
        }
    }
}

// -----------------------------------------------------------------------------
// Open Finding SR1-07
// -----------------------------------------------------------------------------
#[test]
#[should_panic(expected = "SR1-07")]
fn sr1_07_verify_fd_rejects_version_2_header_on_v3_handover() {
    let _lock = FD_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let g = Geometry::new(2, 4, 4, Format::Rgb24).unwrap();
    let mut header = g.header(true);
    // Overwrite header version field (offset 8) to version 2
    header[8..12].copy_from_slice(&2u32.to_le_bytes());

    let seals = libc::F_SEAL_SHRINK | libc::F_SEAL_GROW | libc::F_SEAL_FUTURE_WRITE | libc::F_SEAL_SEAL;
    let fd = create_custom_memfd(&header, g.ring_bytes, seals);
    let h = Handshake {
        geometry: g,
        pid: std::process::id(),
        nonce: 42,
    };
    let uid = unsafe { libc::getuid() };

    let reader = handover::verify_fd(fd, h, uid).expect("verify_fd call");
    assert_eq!(
        reader.version, 3,
        "SR1-07: verify_fd accepted a version 2 header on a version 3 handover"
    );
}

// -----------------------------------------------------------------------------
// Open Finding SR1-08
// -----------------------------------------------------------------------------
#[test]
#[should_panic(expected = "SR1-08")]
fn sr1_08_check_socket_bypassed_by_bind_on_socketpair() {
    let _lock = FD_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    unsafe {
        let mut pair = [0i32; 2];
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

        let path = format!("/tmp/vqa-adv-sockpair-bind-{}", std::process::id());
        let mut addr: libc::sockaddr_un = std::mem::zeroed();
        addr.sun_family = libc::AF_UNIX as _;
        for (i, b) in path.bytes().enumerate() {
            addr.sun_path[i] = b as _;
        }
        let _ = libc::bind(
            a.as_raw_fd(),
            (&addr as *const libc::sockaddr_un).cast(),
            std::mem::size_of_val(&addr) as _,
        );
        let _ = std::fs::remove_file(&path);

        let g = Geometry::new(2, 4, 4, Format::Rgb24).unwrap();
        let writer = Writer::create(g, true).unwrap();
        let uid = libc::getuid();

        let res = handover::send(a.as_fd(), &writer, &[uid], 123);
        assert!(
            res.is_err(),
            "SR1-08: check_socket was bypassed by calling bind on socketpair"
        );
    }
}

// -----------------------------------------------------------------------------
// Open Finding SR1-09
// -----------------------------------------------------------------------------
#[test]
fn sr1_09_auto_wait_backend_spin_path() {
    let _lock = FD_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let g = Geometry::new(2, 4, 4, Format::Rgb24).unwrap();
    let mut writer = Writer::create(g, true).unwrap();
    let mut reader = writer.reader().unwrap();

    let frame = vec![0u8; g.slot_bytes];

    let start = Instant::now();
    let handle = std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(5));
        writer.publish(0, &frame).unwrap();
        writer
    });

    let wait_res = reader.wait(Duration::from_secs(1)).unwrap();
    let elapsed = start.elapsed();
    let _writer = handle.join().unwrap();

    assert!(wait_res, "reader.wait using Auto backend should detect publication");
    println!("VM: Auto wait spin path elapsed time = {:?}", elapsed);
    assert!(
        elapsed < Duration::from_millis(200),
        "VM: wait with Auto backend took longer than expected: {:?}",
        elapsed
    );

    // Drain the published frame so reader.last is updated and head == consumed
    let frames = reader.poll(false, true);
    assert_eq!(frames.len(), 1);

    let start_timeout = Instant::now();
    let timeout_res = reader.wait(Duration::from_millis(10)).unwrap();
    let elapsed_timeout = start_timeout.elapsed();
    assert!(!timeout_res, "reader.wait should return false on timeout when no new frames exist");
    println!("VM: Auto wait timeout path elapsed time = {:?}", elapsed_timeout);

    let mut writer2 = Writer::create(g, true).unwrap();
    let reader2 = writer2.reader().unwrap();
    writer2.close().unwrap();
    let shutdown_res = reader2.wait(Duration::from_millis(100)).unwrap();
    assert!(!shutdown_res, "reader.wait should return false when writer is shut down");
}

// -----------------------------------------------------------------------------
// Additional Attacks / Edge Cases
// -----------------------------------------------------------------------------

#[test]
fn attack_not_a_memfd() {
    let _lock = FD_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let g = Geometry::new(2, 4, 4, Format::Rgb24).unwrap();
    let fd = unsafe {
        OwnedFd::from_raw_fd(libc::open(
            c"/dev/shm".as_ptr(),
            libc::O_TMPFILE | libc::O_RDWR | libc::O_CLOEXEC,
            0o600,
        ))
    };
    assert!(fd.as_raw_fd() >= 0);
    let raw_fd = fd.as_raw_fd();
    let h = Handshake {
        geometry: g,
        pid: std::process::id(),
        nonce: 0,
    };
    let uid = unsafe { libc::getuid() };

    let res = handover::verify_fd(fd, h, uid);
    assert!(matches!(res, Err(RingError::NotMemfd)));
    unsafe {
        assert!(
            libc::fcntl(raw_fd, libc::F_GETFD) < 0,
            "received non-memfd fd was not closed on error"
        );
    }
}

#[test]
fn attack_missing_required_seal() {
    let _lock = FD_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let g = Geometry::new(2, 4, 4, Format::Rgb24).unwrap();
    let header = g.header(true);
    let all_seals = libc::F_SEAL_SHRINK | libc::F_SEAL_GROW | libc::F_SEAL_FUTURE_WRITE | libc::F_SEAL_SEAL;
    let missing_seals = [
        libc::F_SEAL_SHRINK,
        libc::F_SEAL_GROW,
        libc::F_SEAL_FUTURE_WRITE,
        libc::F_SEAL_SEAL,
    ];

    for missing in missing_seals {
        let seals = all_seals & !missing;
        let fd = create_custom_memfd(&header, g.ring_bytes, seals);
        let raw_fd = fd.as_raw_fd();
        let h = Handshake {
            geometry: g,
            pid: std::process::id(),
            nonce: 0,
        };
        let uid = unsafe { libc::getuid() };

        let res = handover::verify_fd(fd, h, uid);
        match res {
            Err(RingError::Unsealed) | Err(RingError::NotMemfd) => {}
            _ => panic!("expected Unsealed or NotMemfd error, got ok or wrong err"),
        }
        unsafe {
            assert!(
                libc::fcntl(raw_fd, libc::F_GETFD) < 0,
                "fd was not closed on missing seal error"
            );
        }
    }
}

#[test]
fn attack_size_mismatch() {
    let _lock = FD_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let g = Geometry::new(2, 4, 4, Format::Rgb24).unwrap();
    let header = g.header(true);
    let all_seals = libc::F_SEAL_SHRINK | libc::F_SEAL_GROW | libc::F_SEAL_FUTURE_WRITE | libc::F_SEAL_SEAL;

    for bad_size in [g.ring_bytes + 4096, g.ring_bytes.saturating_sub(64)] {
        if bad_size == 0 {
            continue;
        }
        let fd = create_custom_memfd(&header, bad_size, all_seals);
        let raw_fd = fd.as_raw_fd();
        let h = Handshake {
            geometry: g,
            pid: std::process::id(),
            nonce: 0,
        };
        let uid = unsafe { libc::getuid() };

        let res = handover::verify_fd(fd, h, uid);
        assert!(
            matches!(res, Err(RingError::Size)),
            "expected Size error for bad size {}",
            bad_size
        );
        unsafe {
            assert!(
                libc::fcntl(raw_fd, libc::F_GETFD) < 0,
                "fd was not closed on size mismatch error"
            );
        }
    }
}

#[test]
fn attack_corrupted_geometry_header() {
    let _lock = FD_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let g = Geometry::new(2, 4, 4, Format::Rgb24).unwrap();
    let mut bad_header = g.header(true);
    bad_header[..8].copy_from_slice(b"BADMAGIC");
    let all_seals = libc::F_SEAL_SHRINK | libc::F_SEAL_GROW | libc::F_SEAL_FUTURE_WRITE | libc::F_SEAL_SEAL;

    let fd = create_custom_memfd(&bad_header, g.ring_bytes, all_seals);
    let raw_fd = fd.as_raw_fd();
    let h = Handshake {
        geometry: g,
        pid: std::process::id(),
        nonce: 0,
    };
    let uid = unsafe { libc::getuid() };

    let res = handover::verify_fd(fd, h, uid);
    assert!(matches!(res, Err(RingError::Format)), "expected Format error");
    unsafe {
        assert!(
            libc::fcntl(raw_fd, libc::F_GETFD) < 0,
            "fd was not closed on corrupted header error"
        );
    }
}

#[test]
fn attack_wrong_nonce() {
    let _lock = FD_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let g = Geometry::new(2, 4, 4, Format::Rgb24).unwrap();
    let writer = Writer::create(g, true).unwrap();
    let uid = unsafe { libc::getuid() };
    let (a, b) = connection();

    let fds_before = count_open_fds();
    handover::send(a.as_fd(), &writer, &[uid], 0x12345678).unwrap();

    let res = handover::recv(
        b.as_fd(),
        &Policy {
            allow_uids: &[uid],
            expected_nonce: Some(0x99999999),
        },
    );
    assert!(matches!(res, Err(RingError::Peer)));
    assert_eq!(
        count_open_fds(),
        fds_before,
        "received fd was not closed on wrong nonce error"
    );
}

#[test]
fn attack_two_fds_in_one_message() {
    let _lock = FD_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let g = Geometry::new(2, 4, 4, Format::Rgb24).unwrap();
    let writer = Writer::create(g, true).unwrap();
    let uid = unsafe { libc::getuid() };
    let (a, b) = connection();

    let h = Handshake {
        geometry: g,
        pid: std::process::id(),
        nonce: 42,
    };
    let fds = [writer.fd().as_raw_fd(), writer.fd().as_raw_fd()];

    let fds_before = count_open_fds();
    send_raw_cmsg(a.as_raw_fd(), &h.encode(), &fds).unwrap();

    let res = handover::recv(
        b.as_fd(),
        &Policy {
            allow_uids: &[uid],
            expected_nonce: Some(42),
        },
    );
    assert!(matches!(res, Err(RingError::Ctrunc)));
    assert_eq!(
        count_open_fds(),
        fds_before,
        "received fds were not closed on multiple fds error"
    );
}

#[test]
fn attack_truncated_control_message() {
    let _lock = FD_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let g = Geometry::new(2, 4, 4, Format::Rgb24).unwrap();
    let uid = unsafe { libc::getuid() };
    let (a, b) = connection();

    let h = Handshake {
        geometry: g,
        pid: std::process::id(),
        nonce: 42,
    };

    let fds_before = count_open_fds();
    unsafe {
        assert_eq!(
            libc::send(a.as_raw_fd(), h.encode().as_ptr().cast(), 64, 0),
            64
        );
    }

    let res = handover::recv(
        b.as_fd(),
        &Policy {
            allow_uids: &[uid],
            expected_nonce: Some(42),
        },
    );
    assert!(matches!(res, Err(RingError::Ctrunc)));
    assert_eq!(
        count_open_fds(),
        fds_before,
        "fd state changed on truncated control message error"
    );
}
