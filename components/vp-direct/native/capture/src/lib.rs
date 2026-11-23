use std::{
    ffi::{c_int, c_void},
    fs, mem,
    net::Shutdown,
    os::{
        fd::{FromRawFd, IntoRawFd},
        unix::net::UnixStream,
    },
    panic::{self, AssertUnwindSafe},
    ptr, slice,
    sync::mpsc::{self, Receiver, SyncSender},
    thread::{self, JoinHandle},
    time::Duration,
};

const OK: i32 = 0;
const INVALID_ARGUMENT: i32 = 1;
const BAD_DISPLAY: i32 = 2;
const BUFFER_TOO_SMALL: i32 = 4;
const X11_ERROR: i32 = 5;
const RESOURCE_ERROR: i32 = 6;
const TIMED_OUT: i32 = 7;
const CLOSED: i32 = 8;
const RGB24: u32 = 1;
const DIGEST_BYTES: usize = 32;
const REPLY_TIMEOUT: Duration = Duration::from_secs(2);

#[repr(C)]
#[derive(Clone, Copy)]
pub struct FrameInfo {
    width: u32,
    height: u32,
    row_stride: u32,
    format_id: u32,
    byte_len: u64,
}

extern "C" {
    fn vp_x11_connect(display: u32, pid: i32) -> c_int;
    fn vp_x11_open(fd: c_int, source: *mut *mut c_void, width: *mut u32, height: *mut u32) -> i32;
    fn vp_x11_pixels(source: *mut c_void) -> *const u8;
    fn vp_x11_close(source: *mut c_void);
    fn SHA256(data: *const u8, len: usize, digest: *mut u8) -> *mut u8;
}

struct Source(*mut c_void);

impl Source {
    fn open(socket: UnixStream) -> Result<(Self, FrameInfo), i32> {
        let mut source = ptr::null_mut();
        let (mut width, mut height) = (0, 0);
        // The transport consumes the socket and publishes source only on success.
        let result =
            unsafe { vp_x11_open(socket.into_raw_fd(), &mut source, &mut width, &mut height) };
        if result != OK {
            return Err(result);
        }
        let row_stride = width * 3;
        Ok((
            Self(source),
            FrameInfo {
                width,
                height,
                row_stride,
                format_id: RGB24,
                byte_len: u64::from(row_stride) * u64::from(height),
            },
        ))
    }

    fn capture(&mut self, info: FrameInfo, target: FrameTarget) -> i32 {
        let pixels = unsafe { vp_x11_pixels(self.0) };
        if pixels.is_null() {
            return X11_ERROR;
        }
        // XShm has completed the write. No request can rewrite this mapping
        // until the same worker handles the next target.
        let input = unsafe {
            slice::from_raw_parts(pixels, info.width as usize * info.height as usize * 4)
        };
        // The requesting foreign call owns both writable regions until the
        // worker replies or has been joined. They cannot alias.
        let output = unsafe { slice::from_raw_parts_mut(target.pixels, info.byte_len as usize) };
        let (source_pixels, _) = input.as_chunks::<4>();
        let (destination_pixels, _) = output.as_chunks_mut::<3>();
        for ([blue, green, red, _], rgb) in source_pixels.iter().copied().zip(destination_pixels) {
            *rgb = [red, green, blue];
        }
        if unsafe { SHA256(output.as_ptr(), output.len(), target.digest) }.is_null() {
            return RESOURCE_ERROR;
        }
        OK
    }
}

impl Drop for Source {
    fn drop(&mut self) {
        unsafe { vp_x11_close(self.0) };
    }
}

struct FrameTarget {
    pixels: *mut u8,
    digest: *mut u8,
}

// SAFETY: A single synchronous ABI caller loans these exclusively to the
// worker. On timeout the caller first shuts down the socket and joins it.
unsafe impl Send for FrameTarget {}

pub struct Session {
    info: FrameInfo,
    requests: Option<SyncSender<FrameTarget>>,
    replies: Receiver<i32>,
    socket: Option<UnixStream>,
    worker: Option<JoinHandle<()>>,
}

impl Session {
    fn start(display: u32, pid: i32) -> Result<Self, i32> {
        if !(90..=65535).contains(&display) || pid <= 0 {
            return Err(BAD_DISPLAY);
        }
        let exe = fs::read_link(format!("/proc/{pid}/exe")).map_err(|_| BAD_DISPLAY)?;
        if exe.file_name().and_then(|name| name.to_str()) != Some("Xvfb") {
            return Err(BAD_DISPLAY);
        }
        // The path is local-only and the transport verifies the socket peer
        // against the PID and effective UID before sending X11 data.
        let fd = unsafe { vp_x11_connect(display, pid) };
        if fd < 0 {
            return Err(BAD_DISPLAY);
        }
        let socket = unsafe { UnixStream::from_raw_fd(fd) };
        let interrupt = socket.try_clone().map_err(|_| RESOURCE_ERROR)?;
        let (ready, start_result) = mpsc::sync_channel(1);
        let (requests, inbox) = mpsc::sync_channel(1);
        let (complete, replies) = mpsc::sync_channel(1);

        let worker = thread::Builder::new()
            .name("vp-capture".into())
            .spawn(move || match Source::open(socket) {
                Ok((mut source, info)) => {
                    if ready.send(Ok(info)).is_err() {
                        return;
                    }
                    while let Ok(target) = inbox.recv() {
                        let status = source.capture(info, target);
                        if complete.send(status).is_err() || status != OK {
                            break;
                        }
                    }
                }
                Err(status) => {
                    let _ = ready.send(Err(status));
                }
            })
            .map_err(|_| RESOURCE_ERROR)?;

        let status = match start_result.recv_timeout(REPLY_TIMEOUT) {
            Ok(Ok(info)) => {
                return Ok(Self {
                    info,
                    requests: Some(requests),
                    replies,
                    socket: Some(interrupt),
                    worker: Some(worker),
                });
            }
            Ok(Err(status)) => status,
            Err(mpsc::RecvTimeoutError::Timeout) => TIMED_OUT,
            Err(mpsc::RecvTimeoutError::Disconnected) => RESOURCE_ERROR,
        };
        let _ = interrupt.shutdown(Shutdown::Both);
        drop(requests);
        let _ = worker.join();
        Err(status)
    }

    fn close(&mut self, interrupt: bool) {
        self.requests.take();
        if let Some(socket) = self.socket.take() {
            if interrupt {
                let _ = socket.shutdown(Shutdown::Both);
            }
        }
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }

    fn next(&mut self, target: FrameTarget) -> i32 {
        let Some(requests) = &self.requests else {
            return CLOSED;
        };
        // At most one call is in flight, so the one-slot queue cannot be full.
        if requests.send(target).is_err() {
            self.close(true);
            return RESOURCE_ERROR;
        }
        let status = match self.replies.recv_timeout(REPLY_TIMEOUT) {
            Ok(status) => status,
            Err(mpsc::RecvTimeoutError::Timeout) => TIMED_OUT,
            Err(mpsc::RecvTimeoutError::Disconnected) => RESOURCE_ERROR,
        };
        if status != OK {
            self.close(true);
        }
        status
    }
}

impl Drop for Session {
    fn drop(&mut self) {
        self.close(false);
    }
}

fn caught<T>(action: impl FnOnce() -> T) -> Result<T, i32> {
    panic::catch_unwind(AssertUnwindSafe(action)).map_err(|payload| {
        // A custom panic payload can itself panic from Drop.
        if let Err(nested) = panic::catch_unwind(AssertUnwindSafe(|| drop(payload))) {
            mem::forget(nested);
        }
        RESOURCE_ERROR
    })
}

/// # Safety
/// Outputs are valid, non-overlapping writable objects. The handle slot is initialized to null.
#[no_mangle]
pub unsafe extern "C" fn vp_capture_start(
    display: u32,
    xvfb_pid: i32,
    out: *mut *mut Session,
    info: *mut FrameInfo,
) -> i32 {
    caught(|| {
        if out.is_null() || info.is_null() || !(*out).is_null() {
            return INVALID_ARGUMENT;
        }
        match Session::start(display, xvfb_pid) {
            Ok(session) => {
                let geometry = session.info;
                let handle = Box::into_raw(Box::new(session));
                info.write(geometry);
                out.write(handle);
                OK
            }
            Err(status) => status,
        }
    })
    .unwrap_or(RESOURCE_ERROR)
}

/// # Safety
/// The handle is live and exclusive. Outputs are writable, exclusive and disjoint through return.
#[no_mangle]
pub unsafe extern "C" fn vp_capture_next_frame(
    capture: *mut Session,
    dst: *mut u8,
    dst_len: usize,
    digest: *mut u8,
) -> i32 {
    let Some(session) = capture.as_mut() else {
        return INVALID_ARGUMENT;
    };
    if dst.is_null() || digest.is_null() {
        return INVALID_ARGUMENT;
    }
    let needed = session.info.byte_len as usize;
    if dst_len < needed {
        return BUFFER_TOO_SMALL;
    }
    let dst_addr = dst as usize;
    let digest_addr = digest as usize;
    let (Some(dst_end), Some(digest_end)) = (
        dst_addr.checked_add(needed),
        digest_addr.checked_add(DIGEST_BYTES),
    ) else {
        return INVALID_ARGUMENT;
    };
    if dst_addr < digest_end && digest_addr < dst_end {
        return INVALID_ARGUMENT;
    }
    match caught(|| {
        session.next(FrameTarget {
            pixels: dst,
            digest,
        })
    }) {
        Ok(status) => status,
        Err(_) => {
            // A foreign writable loan cannot return while the worker can use it.
            session.close(true);
            RESOURCE_ERROR
        }
    }
}

/// # Safety
/// The slot is live and exclusive and contains null or a previously returned live handle.
#[no_mangle]
pub unsafe extern "C" fn vp_capture_stop(capture: *mut *mut Session) -> i32 {
    let Some(slot) = capture.as_mut() else {
        return INVALID_ARGUMENT;
    };
    let handle = std::mem::replace(slot, ptr::null_mut());
    if !handle.is_null() {
        let _ = caught(|| drop(Box::from_raw(handle)));
    }
    OK
}
