// SPDX-License-Identifier: AGPL-3.0-only
use super::*;
use std::{
    ffi::c_char,
    fs::{self, File},
    io::Write,
    mem::MaybeUninit,
    process::Command,
    sync::{
        atomic::{AtomicU64, Ordering},
        mpsc,
    },
    thread,
    time::Duration,
};

extern "C" {
    fn memfd_create(name: *const c_char, flags: u32) -> c_int;
    fn fcntl(fd: c_int, command: c_int, ...) -> c_int;
}

const SEALS: i32 = 2 | 4 | 8;

fn memfd(label: &std::ffi::CStr, bytes: &[u8], seals: i32) -> File {
    let raw = unsafe { memfd_create(label.as_ptr(), 3) };
    assert!(raw >= 0);
    let mut file = unsafe { File::from_raw_fd(raw) };
    file.write_all(bytes).unwrap();
    assert_eq!(unsafe { fcntl(raw, 1033, seals) }, 0);
    file
}

fn resources(label: &str) -> (usize, usize) {
    let fds = fs::read_dir("/proc/self/fd")
        .unwrap()
        .filter_map(Result::ok)
        .filter_map(|entry| fs::read_link(entry.path()).ok())
        .filter(|link| link.to_string_lossy().contains(label))
        .count();
    let maps = fs::read_to_string("/proc/self/maps")
        .unwrap()
        .lines()
        .filter(|line| line.contains(label))
        .count();
    (fds, maps)
}

fn get_api() -> Api {
    let mut table = MaybeUninit::zeroed();
    assert_eq!(unsafe { bootstrap(table.as_mut_ptr()) }, OK);
    unsafe { table.assume_init() }
}

unsafe fn bootstrap(out: *mut Api) -> i32 {
    unsafe {
        vp_bulk_get_api_v1(
            ABI,
            size_of::<Api>() as u32,
            size_of::<Intent>() as u32,
            size_of::<Lease>() as u32,
            size_of::<Block>() as u32,
            size_of::<View>() as u32,
            out,
        )
    }
}

fn identity() -> Lease {
    static NEXT: AtomicU64 = AtomicU64::new(100);
    let id = NEXT.fetch_add(1, Ordering::Relaxed);
    Lease {
        session_id: id,
        lane_id: id,
        generation: 1,
        reference: 0,
        epoch: 1,
    }
}

fn intent() -> Intent {
    Intent {
        consumer_id: 1,
        deadline_monotonic_ns: unsafe { vp_bulk_monotonic_ns() } + 10_000_000_000,
        affinity_group: 2,
        expected_order: 0,
        priority: 0,
        flags: HAS_PRIORITY,
    }
}

fn block(lease: Lease, fd: i32, length: u64) -> Block {
    Block {
        lease,
        offset: 0,
        length,
        skipped: 0,
        kind: BYTES,
        width: 0,
        height: 0,
        row_stride: 0,
        format_id: 0,
        fd,
    }
}

fn frame(lease: Lease, fd: i32) -> Block {
    Block {
        kind: FRAME,
        width: 1,
        height: 1,
        row_stride: 3,
        format_id: super::super::RGB24,
        ..block(lease, fd, 3)
    }
}

fn offer(api: &Api, lease: Lease, intent: Intent, blocks: &[Block]) -> (i32, u32) {
    let mut used = u32::MAX;
    let data = if blocks.is_empty() {
        ptr::null()
    } else {
        blocks.as_ptr()
    };
    let status = unsafe {
        (api.publish_batch)(
            api.context,
            lease.lane_id,
            &intent,
            data,
            blocks.len() as u32,
            &mut used,
        )
    };
    (status, used)
}

fn take_batch(
    api: &Api,
    lease: Lease,
    intent: Intent,
    delivery: u32,
    output: &mut [Block],
) -> (i32, u32) {
    let mut used = u32::MAX;
    let data = if output.is_empty() {
        ptr::null_mut()
    } else {
        output.as_mut_ptr()
    };
    let status = unsafe {
        (api.borrow_batch)(
            api.context,
            lease.lane_id,
            &intent,
            delivery,
            data,
            output.len() as u32,
            &mut used,
        )
    };
    (status, used)
}

fn view(api: &Api, lease: Lease) -> (i32, View) {
    let mut out = View {
        data: NonNull::dangling().as_ptr(),
        length: 7,
    };
    let status = unsafe { (api.cpu_view)(api.context, &lease, &mut out) };
    (status, out)
}

fn bytes(view: &View) -> &[u8] {
    if view.length == 0 {
        return &[];
    }
    assert!(!view.data.is_null());
    unsafe { slice::from_raw_parts(view.data, view.length as usize) }
}

fn drain(api: &Api, id: Lease) -> i32 {
    unsafe { (api.break_and_drain)(api.context, id.session_id) }
}

fn retire(api: &Api, id: Lease) -> i32 {
    unsafe { (api.release)(api.context, &id) }
}

#[test]
fn empty_and_nonempty_exact_views() {
    let api = get_api();
    let id = identity();
    let empty = memfd(c"vp-test-empty", b"", SEALS);
    let original = memfd(c"vp-test-extent", b"xabc", SEALS);
    let source = Block {
        offset: 1,
        ..block(id, original.as_raw_fd(), 3)
    };
    assert_eq!(
        offer(
            &api,
            id,
            intent(),
            &[block(id, empty.as_raw_fd(), 0), source]
        ),
        (OK, 2)
    );
    let mut output = [block(id, -1, 0); 2];
    assert_eq!(
        take_batch(&api, id, intent(), ORDERED, &mut output),
        (OK, 2)
    );
    let (status, empty_view) = view(&api, output[0].lease);
    assert_eq!(status, OK);
    assert!(empty_view.data.is_null());
    assert_eq!(bytes(&empty_view), b"");
    let (status, view) = view(&api, output[1].lease);
    assert_eq!(status, OK);
    assert_eq!(bytes(&view), b"abc");
    assert_eq!(retire(&api, output[0].lease), OK);
    assert_eq!(retire(&api, output[1].lease), OK);
    assert_eq!(drain(&api, id), OK);
}

#[test]
fn rejects_missing_each_seal_wrong_extent_kind_and_geometry() {
    let api = get_api();
    let id = identity();
    for missing in [2, 4, 8] {
        let fd = memfd(c"vp-test-missing-seal", b"abc", SEALS & !missing);
        assert_eq!(
            offer(&api, id, intent(), &[frame(id, fd.as_raw_fd())]),
            (INVALID, 0)
        );
        assert_eq!(resources("vp-test-missing-seal"), (1, 0));
    }
    let good = memfd(c"vp-test-batch-rollback", b"abc", SEALS);
    let good_frame = frame(id, good.as_raw_fd());
    for invalid in [
        Block {
            length: 1,
            ..block(id, good.as_raw_fd(), 3)
        },
        Block {
            offset: isize::MAX as u64,
            ..block(id, good.as_raw_fd(), 3)
        },
        Block {
            width: u32::MAX,
            ..good_frame
        },
        Block {
            format_id: 0,
            ..good_frame
        },
        Block {
            length: 0,
            ..good_frame
        },
        Block {
            kind: END,
            ..good_frame
        },
        Block {
            fd: -1,
            ..block(id, -1, 0)
        },
        Block {
            kind: 99,
            ..good_frame
        },
    ] {
        assert_eq!(
            offer(&api, id, intent(), &[good_frame, invalid]),
            (INVALID, 0)
        );
        assert_eq!(resources("vp-test-batch-rollback"), (1, 0));
    }
    let mut output = [block(id, -1, 0)];
    assert_eq!(
        take_batch(&api, id, intent(), ORDERED, &mut output),
        (AGAIN, 0)
    );
    assert_eq!(drain(&api, id), OK);
}

#[test]
fn prefix_backpressure_custody_and_restart_balance() {
    let api = get_api();
    let id = identity();
    let original = memfd(c"vp-test-prefix", b"x", SEALS);
    let candidate = block(id, original.as_raw_fd(), 1);
    assert_eq!(offer(&api, id, intent(), &[]), (OK, 0));
    assert_eq!(take_batch(&api, id, intent(), ORDERED, &mut []), (OK, 0));
    assert_eq!(
        offer(&api, id, intent(), &[candidate; CAPACITY + 1]),
        (AGAIN, CAPACITY as u32)
    );
    assert_eq!(offer(&api, id, intent(), &[candidate]), (AGAIN, 0));
    assert_eq!(resources("vp-test-prefix"), (CAPACITY + 1, CAPACITY));
    drop(original);
    let mut output = [block(id, -1, 0); CAPACITY];
    assert_eq!(
        take_batch(&api, id, intent(), ORDERED, &mut output),
        (OK, CAPACITY as u32)
    );
    for delivery in output {
        assert_eq!(unsafe { fcntl(delivery.fd, 1) }, 1);
        assert_eq!(bytes(&view(&api, delivery.lease).1), b"x");
        assert_eq!(retire(&api, delivery.lease), OK);
        assert_eq!(unsafe { fcntl(delivery.fd, 1) }, -1);
    }
    assert_eq!(resources("vp-test-prefix"), (0, 0));
    assert_eq!(drain(&api, id), OK);
    let new_id = Lease {
        lane_id: id.lane_id,
        ..identity()
    };
    let new_fd = memfd(c"vp-test-prefix", b"z", SEALS);
    assert_eq!(
        offer(
            &api,
            new_id,
            intent(),
            &[block(new_id, new_fd.as_raw_fd(), 1)]
        ),
        (OK, 1)
    );
    assert_eq!(retire(&api, output[0].lease), ALREADY_RELEASED);
    assert_eq!(drain(&api, new_id), OK);
    drop(new_fd);
    assert_eq!(resources("vp-test-prefix"), (0, 0));
}

#[test]
fn intent_failures_and_deadline_leave_outputs_and_originals_consistent() {
    let api = get_api();
    let id = identity();
    let fd = memfd(c"vp-test-intent", b"x", SEALS);
    let candidate = block(id, fd.as_raw_fd(), 1);
    for (bad, error) in [
        (
            Intent {
                deadline_monotonic_ns: 1,
                ..intent()
            },
            DEADLINE,
        ),
        (
            Intent {
                flags: 0,
                ..intent()
            },
            INVALID,
        ),
        (
            Intent {
                affinity_group: 0,
                ..intent()
            },
            INVALID,
        ),
        (
            Intent {
                consumer_id: 0,
                ..intent()
            },
            INVALID,
        ),
        (
            Intent {
                flags: HAS_PRIORITY | HAS_EXPECTED_ORDER,
                expected_order: 1,
                ..intent()
            },
            STALE,
        ),
    ] {
        assert_eq!(offer(&api, id, bad, &[candidate]), (error, 0));
        assert_eq!(resources("vp-test-intent"), (1, 0));
    }
    assert_eq!(offer(&api, id, intent(), &[candidate]), (OK, 1));
    let mut output = [block(id, -1, 0)];
    let expired = Intent {
        deadline_monotonic_ns: 1,
        ..intent()
    };
    assert_eq!(
        take_batch(&api, id, expired, ORDERED, &mut output),
        (DEADLINE, 0)
    );
    assert_eq!(
        take_batch(
            &api,
            id,
            Intent {
                consumer_id: 4,
                ..intent()
            },
            ORDERED,
            &mut output
        ),
        (INVALID, 0)
    );
    assert_eq!(
        take_batch(&api, id, intent(), 99, &mut output),
        (INVALID, 0)
    );
    assert_eq!(output[0].fd, -1);
    assert_eq!(
        take_batch(&api, id, intent(), ORDERED, &mut output),
        (OK, 1)
    );
    assert_eq!(retire(&api, output[0].lease), OK);
    assert_eq!(drain(&api, id), OK);
}

#[test]
fn latest_ordered_and_end_epoch_barrier() {
    let api = get_api();
    let id = identity();
    let first = memfd(c"vp-test-first", b"old", SEALS);
    let second = memfd(c"vp-test-second", b"new", SEALS);
    let next = Lease { epoch: 2, ..id };
    let end = Block {
        kind: END,
        ..block(id, -1, 0)
    };
    assert_eq!(
        offer(
            &api,
            id,
            intent(),
            &[
                frame(id, first.as_raw_fd()),
                frame(id, second.as_raw_fd()),
                end,
                frame(next, first.as_raw_fd()),
                frame(next, second.as_raw_fd()),
            ]
        ),
        (OK, 5)
    );
    assert_eq!(
        offer(&api, id, intent(), &[frame(id, first.as_raw_fd())]),
        (STALE, 0)
    );
    let mut output = [block(id, -1, 0); 4];
    assert_eq!(take_batch(&api, id, intent(), LATEST, &mut output), (OK, 1));
    let last = output[0].lease;
    assert_eq!(output[0].skipped, 1);
    assert_eq!(bytes(&view(&api, last).1), b"new");
    assert_eq!(
        take_batch(&api, id, intent(), ORDERED, &mut output),
        (AGAIN, 0)
    );
    assert_eq!(retire(&api, last), OK);
    assert_eq!(take_batch(&api, id, intent(), LATEST, &mut output), (OK, 1));
    assert_eq!(output[0].kind, END);
    assert_eq!(view(&api, output[0].lease).0, INVALID);
    assert_eq!(view(&api, last).0, STALE);
    assert_eq!(retire(&api, output[0].lease), OK);
    assert_eq!(
        take_batch(&api, next, intent(), ORDERED, &mut output),
        (OK, 2)
    );
    assert_eq!(bytes(&view(&api, output[0].lease).1), b"old");
    assert_eq!(bytes(&view(&api, output[1].lease).1), b"new");
    assert_eq!(retire(&api, output[0].lease), OK);
    assert_eq!(retire(&api, output[1].lease), OK);
    assert_eq!(drain(&api, id), OK);
}

#[test]
fn concurrent_break_pins_existing_view_and_exact_release() {
    let api = get_api();
    let id = identity();
    let fd = memfd(c"vp-test-drain", b"view-canary", SEALS);
    let candidate = block(id, fd.as_raw_fd(), 11);
    assert_eq!(offer(&api, id, intent(), &[candidate, candidate]), (OK, 2));
    let mut output = [block(id, -1, 0)];
    assert_eq!(
        take_batch(&api, id, intent(), ORDERED, &mut output),
        (OK, 1)
    );
    let live = output[0].lease;
    let (status, issued) = view(&api, live);
    assert_eq!(status, OK);
    let (ready, paused) = mpsc::channel();
    let worker = thread::spawn(move || ready.send(drain(&get_api(), id)).unwrap());
    assert_eq!(paused.recv_timeout(Duration::from_secs(3)).unwrap(), AGAIN);
    worker.join().unwrap();
    assert_eq!(bytes(&issued), b"view-canary");
    assert_eq!(resources("vp-test-drain"), (2, 1));
    assert_eq!(offer(&api, id, intent(), &[candidate]), (BROKEN, 0));
    assert_eq!(
        take_batch(&api, id, intent(), ORDERED, &mut output),
        (BROKEN, 0)
    );
    let (status, refused) = view(&api, live);
    assert_eq!(status, BROKEN);
    assert_eq!((refused.data, refused.length), (ptr::null(), 0));
    assert_eq!(
        retire(
            &api,
            Lease {
                generation: live.generation + 1,
                ..live
            }
        ),
        STALE
    );
    assert_eq!(drain(&api, id), AGAIN);
    assert_eq!(retire(&api, live), OK);
    assert_eq!(retire(&api, live), ALREADY_RELEASED);
    assert_eq!(drain(&api, id), OK);
    assert_eq!(drain(&api, id), OK);
    assert_eq!(resources("vp-test-drain"), (1, 0));
}

#[test]
fn drain_fences_inflight_prepare_and_waits_for_rollback() {
    let id = identity();
    let (prepared, ready) = mpsc::channel();
    let (resume, proceed) = mpsc::channel();
    let worker = thread::spawn(move || {
        let fd = memfd(c"vp-test-inflight", b"x", SEALS);
        PUBLISH_HOOK.with(|hook| {
            hook.replace(Some(Box::new(move || {
                prepared.send(()).unwrap();
                proceed.recv_timeout(Duration::from_secs(5)).unwrap();
            })));
        });
        offer(&get_api(), id, intent(), &[block(id, fd.as_raw_fd(), 1)])
    });
    ready.recv_timeout(Duration::from_secs(3)).unwrap();
    let api = get_api();
    assert_eq!(resources("vp-test-inflight"), (2, 1));
    assert_eq!(drain(&api, id), AGAIN);
    resume.send(()).unwrap();
    assert_eq!(worker.join().unwrap(), (BROKEN, 0));
    assert_eq!(drain(&api, id), OK);
    assert_eq!(resources("vp-test-inflight"), (0, 0));
}

#[test]
fn panic_at_each_dispatch_fences_only_affected_session_and_preserves_live_custody() {
    let api = get_api();
    let fd = memfd(c"vp-test-panic", b"panic", SEALS);
    let unrelated = identity();
    assert_eq!(
        offer(
            &api,
            unrelated,
            intent(),
            &[block(unrelated, fd.as_raw_fd(), 5)]
        ),
        (OK, 1)
    );
    for point in [
        Fault::Publish,
        Fault::Borrow,
        Fault::View,
        Fault::Release,
        Fault::Drain,
    ] {
        let id = identity();
        let candidate = block(id, fd.as_raw_fd(), 5);
        assert_eq!(offer(&api, id, intent(), &[candidate, candidate]), (OK, 2));
        let mut output = [block(id, -1, 0)];
        assert_eq!(
            take_batch(&api, id, intent(), ORDERED, &mut output),
            (OK, 1)
        );
        let held = output[0].lease;
        let (status, existing) = view(&api, held);
        assert_eq!(status, OK);
        INJECT_FAULT.set(Some(point));
        match point {
            Fault::Publish => assert_eq!(offer(&api, id, intent(), &[candidate]), (BROKEN, 0)),
            Fault::Borrow => {
                output[0].fd = -42;
                assert_eq!(
                    take_batch(&api, id, intent(), ORDERED, &mut output),
                    (BROKEN, 0)
                );
                assert_eq!(output[0].fd, -42);
            }
            Fault::View => {
                let (rc, view) = view(&api, held);
                assert_eq!((rc, view.data, view.length), (BROKEN, ptr::null(), 0));
            }
            Fault::Release => assert_eq!(retire(&api, held), BROKEN),
            Fault::Drain => assert_eq!(drain(&api, id), BROKEN),
            Fault::Bootstrap => unreachable!(),
        }
        assert_eq!(resources("vp-test-panic"), (3, 2));
        assert_eq!(bytes(&existing), b"panic");
        assert_eq!(offer(&api, id, intent(), &[candidate]), (BROKEN, 0));
        assert_eq!(retire(&api, held), OK);
        assert_eq!(drain(&api, id), OK);
    }
    let mut output = [block(unrelated, -1, 0)];
    assert_eq!(
        take_batch(&api, unrelated, intent(), ORDERED, &mut output),
        (OK, 1)
    );
    assert_eq!(retire(&api, output[0].lease), OK);
    assert_eq!(drain(&api, unrelated), OK);
}

#[test]
fn bootstrap_panic_keeps_zero_table_and_fences_without_revoking_live_views() {
    const CHILD: &str = "VP_U_BOOTSTRAP_PANIC_CHILD";
    if std::env::var_os(CHILD).is_none() {
        assert!(Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "bulk::tests::bootstrap_panic_keeps_zero_table_and_fences_without_revoking_live_views"])
            .env(CHILD, "1").status().unwrap().success());
        return;
    }
    let api = get_api();
    let id = identity();
    let fd = memfd(c"vp-test-bootstrap", b"x", SEALS);
    let item = block(id, fd.as_raw_fd(), 1);
    assert_eq!(offer(&api, id, intent(), &[item]), (OK, 1));
    let mut output = [block(id, -1, 0)];
    assert_eq!(
        take_batch(&api, id, intent(), ORDERED, &mut output),
        (OK, 1)
    );
    let held = output[0].lease;
    let (status, issued) = view(&api, held);
    assert_eq!(status, OK);
    INJECT_FAULT.set(Some(Fault::Bootstrap));
    let mut table = [0u64; size_of::<Api>() / size_of::<u64>()];
    assert_eq!(unsafe { bootstrap(table.as_mut_ptr().cast()) }, BROKEN);
    assert!(table.iter().all(|value| *value == 0));
    assert_eq!(offer(&api, id, intent(), &[item]), (BROKEN, 0));
    assert_eq!(bytes(&issued), b"x");
    assert_eq!(retire(&api, held), OK);
    assert_eq!(drain(&api, id), OK);
}

#[test]
fn deadline_expiring_after_duplicate_rolls_back_before_commit() {
    let api = get_api();
    let id = identity();
    let fd = memfd(c"vp-test-expired-after-dup", b"x", SEALS);
    let candidate = block(id, fd.as_raw_fd(), 1);
    let expired = Intent {
        deadline_monotonic_ns: unsafe { vp_bulk_monotonic_ns() } + 150_000_000,
        ..intent()
    };
    PUBLISH_HOOK.with(|hook| {
        hook.replace(Some(Box::new(move || {
            assert_eq!(resources("vp-test-expired-after-dup"), (2, 1));
            while unsafe { vp_bulk_monotonic_ns() } < expired.deadline_monotonic_ns {
                thread::sleep(Duration::from_millis(1));
            }
        })));
    });
    assert_eq!(offer(&api, id, expired, &[candidate]), (DEADLINE, 0));
    PUBLISH_HOOK.with(|hook| assert!(hook.borrow().is_none()));
    assert_eq!(resources("vp-test-expired-after-dup"), (1, 0));
    assert_eq!(offer(&api, id, intent(), &[candidate]), (OK, 1));
    assert_eq!(drain(&api, id), OK);
}
