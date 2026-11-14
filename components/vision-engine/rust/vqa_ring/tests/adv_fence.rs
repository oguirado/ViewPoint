//! Adversarial integration tests for vqa_ring publication and fence guarantees (O5 zero torn frames accepted).
//!
//! Test policy for bugs:
//! Rust has no expectedFailure feature. If a test asserts correct behavior for a known bug,
//! the assertion message starts with the finding ID (e.g., "FINDING-O5-001: ...") and the test
//! is marked with `#[should_panic(expected = "FINDING-O5-001")]`. When the underlying bug is fixed,
//! the test will fail (because it no longer panics) forcing the removal or update of the test marker.

use std::{
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc,
    },
    thread,
};
use vqa_ring::{Format, Geometry, ReadResult, Writer};

fn make_geometry(slots: u32) -> Geometry {
    Geometry::new(slots, 8, 8, Format::Rgb24).expect("valid geometry")
}

fn expected_bytes(r: u64, slot_bytes: usize) -> Vec<u8> {
    (0..slot_bytes)
        .map(|i| ((r.wrapping_mul(131).wrapping_add((i as u64).wrapping_mul(7)).wrapping_add(19)) & 0xFF) as u8)
        .collect()
}

fn verify_frame_data(r: u64, bytes: &[u8]) -> bool {
    let expected = expected_bytes(r, bytes.len());
    bytes == expected.as_slice()
}

#[test]
fn test_abandoned_begin_without_commit() {
    let g = make_geometry(2);
    let mut writer = Writer::create(g, true).expect("writer creation");
    let reader = writer.reader().expect("reader creation");

    // Begin slot for render index 0, write partial/torn data, but do NOT commit.
    writer.begin(0).expect("begin 0");
    writer.write(&expected_bytes(0, g.slot_bytes)).expect("write 0");

    // Reader attempts to read r=0 while begin is active (uncommitted).
    let res = reader.try_read(0, true);
    assert!(
        matches!(res, ReadResult::Torn),
        "uncommitted frame must return Torn, got {:?}",
        res
    );

    // Also verify acquire returns Torn
    let mut mut_reader = writer.reader().expect("reader creation");
    let res_acq = mut_reader.acquire(0, true);
    assert!(
        matches!(res_acq, ReadResult::Torn),
        "uncommitted acquire must return Torn, got {:?}",
        res_acq
    );

    // Verify writer prevents new begin when active is uncommitted.
    assert!(writer.begin(1).is_err(), "begin while active must fail");
}

#[test]
fn test_slot_overwritten_during_read() {
    let g = make_geometry(2);
    let mut writer = Writer::create(g, true).expect("writer creation");
    let reader = writer.reader().expect("reader creation");

    // Publish frame 0
    let f0_bytes = expected_bytes(0, g.slot_bytes);
    writer.publish(0, &f0_bytes).expect("publish 0");

    // Reader reads frame 0
    let ReadResult::Frame(frame0) = reader.try_read(0, true) else {
        panic!("expected frame 0");
    };

    assert!(verify_frame_data(0, &frame0.bytes));
    assert!(frame0.valid(), "frame0 should be valid before overwrite");

    // Overwrite slot 0 by publishing frame 1 (slot 1) and then frame 2 (slot 0)
    writer.publish(1, &expected_bytes(1, g.slot_bytes)).expect("publish 1");
    writer.publish(2, &expected_bytes(2, g.slot_bytes)).expect("publish 2");

    // frame0 validity check must now be false
    assert!(!frame0.valid(), "frame0 should no longer be valid after overwrite");

    // Trying to read frame 0 now must return Lapped (since head is 3, slots=2, 3 - 0 >= 2)
    let res0 = reader.try_read(0, true);
    assert!(
        matches!(res0, ReadResult::Lapped),
        "reading overwritten index 0 must return Lapped, got {:?}",
        res0
    );

    // Reading frame 2 must yield valid frame 2
    let ReadResult::Frame(frame2) = reader.try_read(2, true) else {
        panic!("expected frame 2");
    };
    assert!(verify_frame_data(2, &frame2.bytes));
    assert!(frame2.valid());
}

#[test]
fn test_concurrent_writer_readers_fence_o5() {
    run_adv_scenario(2, true, true, 2000);
    run_adv_scenario(3, false, false, 2000);
}

#[test]
fn test_wrap_many_laps_2_and_3_slots() {
    run_adv_scenario(2, true, false, 3000);
    run_adv_scenario(3, true, true, 3000);
}

#[test]
fn test_verify_on_and_off_matrix() {
    for writer_crc in [true, false] {
        for reader_verify in [true, false] {
            run_adv_scenario(2, writer_crc, reader_verify, 1000);
            run_adv_scenario(3, writer_crc, reader_verify, 1000);
        }
    }
}

fn run_adv_scenario(slots: u32, writer_crc: bool, reader_verify: bool, num_frames: u64) {
    let g = make_geometry(slots);
    let mut writer = Writer::create(g, writer_crc).expect("writer creation");
    let reader1 = writer.reader().expect("reader 1");
    let reader2 = writer.reader().expect("reader 2");
    let reader3 = writer.reader().expect("reader 3");

    let published_head = Arc::new(AtomicU64::new(0));
    let stop_signal = Arc::new(AtomicBool::new(false));

    let p_head_writer = published_head.clone();
    let stop_writer = stop_signal.clone();

    // Writer thread
    let writer_handle = thread::spawn(move || {
        for r in 0..num_frames {
            let bytes = expected_bytes(r, g.slot_bytes);
            writer.publish(r, &bytes).expect("publish frame");
            p_head_writer.store(r + 1, Ordering::Release);
            if r % 50 == 0 {
                thread::yield_now();
            }
        }
        stop_writer.store(true, Ordering::Release);
    });

    // Reader 1 thread: try_read
    let p_head_r1 = published_head.clone();
    let stop_r1 = stop_signal.clone();
    let r1_handle = thread::spawn(move || {
        let mut local_r = 0u64;
        while !stop_r1.load(Ordering::Acquire) || local_r < p_head_r1.load(Ordering::Acquire) {
            let head = p_head_r1.load(Ordering::Acquire);
            if local_r >= head {
                thread::yield_now();
                continue;
            }

            let res = reader1.try_read(local_r, reader_verify);
            match res {
                ReadResult::Frame(ref f) => {
                    assert_eq!(f.render_index, local_r);
                    assert!(
                        verify_frame_data(f.render_index, &f.bytes),
                        "O5 ZERO TORN FRAMES: Frame bytes corrupted for render_index {}",
                        f.render_index
                    );
                    local_r += 1;
                }
                ReadResult::Absent => {
                    let cur_head = p_head_r1.load(Ordering::Acquire);
                    let overwritten = cur_head > local_r + (slots as u64);
                    assert!(
                        overwritten,
                        "Absent returned for published index {} when head is {} and slots={}",
                        local_r, cur_head, slots
                    );
                    local_r += 1;
                }
                ReadResult::Lapped => {
                    let cur_head = p_head_r1.load(Ordering::Acquire);
                    assert!(
                        cur_head >= local_r + (slots as u64),
                        "Lapped returned for index {} when writer head is {} and slots={}",
                        local_r, cur_head, slots
                    );
                    local_r += 1;
                }
                ReadResult::Torn => {
                    thread::yield_now();
                }
            }
        }
    });

    // Reader 2 thread: acquire
    let p_head_r2 = published_head.clone();
    let stop_r2 = stop_signal.clone();
    let r2_handle = thread::spawn(move || {
        let mut mut_reader2 = reader2;
        let mut local_r = 0u64;
        while !stop_r2.load(Ordering::Acquire) || local_r < p_head_r2.load(Ordering::Acquire) {
            let head = p_head_r2.load(Ordering::Acquire);
            if local_r >= head {
                thread::yield_now();
                continue;
            }

            let res = mut_reader2.acquire(local_r, reader_verify);
            match res {
                ReadResult::Frame(ref f) => {
                    assert_eq!(f.render_index, local_r);
                    assert!(
                        verify_frame_data(f.render_index, &f.bytes),
                        "O5 ZERO TORN FRAMES: Frame bytes corrupted in acquire for index {}",
                        f.render_index
                    );
                    local_r += 1;
                }
                ReadResult::Absent => {
                    let cur_head = p_head_r2.load(Ordering::Acquire);
                    let overwritten = cur_head > local_r + (slots as u64);
                    assert!(
                        overwritten,
                        "Absent returned in acquire for published index {} when head is {} and slots={}",
                        local_r, cur_head, slots
                    );
                    local_r += 1;
                }
                ReadResult::Lapped => {
                    let cur_head = p_head_r2.load(Ordering::Acquire);
                    assert!(
                        cur_head >= local_r + (slots as u64),
                        "Lapped returned in acquire for index {} when head is {} and slots={}",
                        local_r, cur_head, slots
                    );
                    local_r += 1;
                }
                ReadResult::Torn => {
                    thread::yield_now();
                }
            }
        }
    });

    // Reader 3 thread: poll latest
    let stop_r3 = stop_signal.clone();
    let r3_handle = thread::spawn(move || {
        let mut mut_reader3 = reader3;
        while !stop_r3.load(Ordering::Acquire) {
            let frames = mut_reader3.poll(true, reader_verify);
            for f in frames {
                assert!(
                    verify_frame_data(f.render_index, &f.bytes),
                    "O5 ZERO TORN FRAMES: Poll latest returned torn frame for index {}",
                    f.render_index
                );
            }
            thread::yield_now();
        }
    });

    writer_handle.join().expect("writer join");
    r1_handle.join().expect("r1 join");
    r2_handle.join().expect("r2 join");
    r3_handle.join().expect("r3 join");
}
