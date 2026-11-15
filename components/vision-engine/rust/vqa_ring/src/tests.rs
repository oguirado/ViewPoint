use crate::*;
use std::{
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc,
    },
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
#[test]
fn layout_overflow_and_formats() {
    code(
        Geometry::new(u32::MAX, u32::MAX, u32::MAX, Format::Bgrx32),
        "E_RING_SIZE",
    );
    code(Geometry::new(0, 1, 1, Format::Rgb24), "E_RING_SIZE");
    let g = geometry();
    let mut h = g.header(true);
    assert_eq!(Geometry::parse(&h).unwrap().0, g);
    h[60] = 128;
    code(Geometry::parse(&h), "E_RING_FORMAT");
    h = g.header(true);
    h[24] = 1;
    code(Geometry::parse(&h), "E_RING_SIZE");
    let mut w = Writer::create(g, true).unwrap();
    assert!(w.commit().is_err());
    assert!(w.publish(0, &[]).is_err());
    w.begin(0).unwrap();
    assert!(w.commit().is_err());
    w.write(&frame(0, g)).unwrap();
    w.commit().unwrap();
    assert!(w.publish(0, &frame(0, g)).is_err());
    assert!(w.begin(u64::MAX).is_err());
}

#[test]
fn o4_odd_and_crc() {
    let g = geometry();
    let mut w = Writer::create(g, true).unwrap();
    let r = w.reader().unwrap();
    w.begin(0).unwrap();
    w.write(&frame(0, g)).unwrap();
    assert!(matches!(r.try_read(0, false), ReadResult::Torn));
    w.commit().unwrap();
    let ReadResult::Frame(f) = r.try_read(0, true) else {
        panic!("committed missing")
    };
    assert!(f.valid());
    w.map.store(g.payload(0), &[255]);
    assert!(matches!(r.try_read(0, true), ReadResult::Torn));
    w.publish(2, &frame(2, g)).unwrap();
    assert!(!f.valid());
}

#[test]
fn o4_torn_poll() {
    let g = geometry();
    let mut w = Writer::create(g, false).unwrap();
    let mut r = w.reader().unwrap();
    w.publish(0, &frame(0, g)).unwrap();
    w.begin(2).unwrap();
    w.write(&frame(2, g)).unwrap();
    assert!(r.poll(false, false).is_empty());
    assert_eq!(r.torn, vec![0]);
}
#[test]
fn head_publication_order() {
    crate::map::test_pin(2);
    let g = Geometry::new(2, 1, 1, Format::Rgb24).unwrap();
    let mut w = Writer::create(g, false).unwrap();
    let reader = w.reader().unwrap();
    let ack = Arc::new(AtomicU64::new(0));
    let done = Arc::new(AtomicBool::new(false));
    let a = ack.clone();
    let d = done.clone();
    let t = std::thread::spawn(move || {
        crate::map::test_pin(3);
        let deadline = Instant::now() + Duration::from_secs(30);
        for r in 0..10_000 {
            while a.load(Ordering::Acquire) != r {
                assert!(Instant::now() < deadline, "ack timeout");
                std::hint::spin_loop();
            }
            w.publish(r, &[r as u8; 3]).unwrap();
        }
        while a.load(Ordering::Acquire) != 10_000 {
            assert!(Instant::now() < deadline);
        }
        d.store(true, Ordering::Release);
    });
    let deadline = Instant::now() + Duration::from_secs(35);
    let mut r = 0;
    while !done.load(Ordering::Acquire) {
        assert!(Instant::now() < deadline);
        if reader.head() > r {
            assert!(
                matches!(reader.try_read(r, false), ReadResult::Frame(_)),
                "head visible before even seq at {r}"
            );
            r += 1;
            ack.store(r, Ordering::Release);
        }
    }
    t.join().unwrap();
    assert_eq!(r, 10_000);
}
// Shared memfd plus fork ensures FUTEX_PRIVATE cannot wake the consumer.

#[test]
#[ignore = "run via tools/run_mutants.py with VQA_STRESS=1"]
fn ri_fence_stress() {
    assert!(
        std::env::var_os("VQA_STRESS").is_some(),
        "VQA_STRESS=1 required"
    );
    assert_eq!(std::env::consts::ARCH, "aarch64");
    crate::map::test_pin(3);
    let g = Geometry::new(1, 1, 1, Format::Rgb24).unwrap();
    let mut w = Writer::create(g, false).unwrap();
    w.publish(0, &[0; 3]).unwrap();
    let r = w.reader().unwrap();
    let stop = Arc::new(AtomicBool::new(false));
    let s = stop.clone();
    let thread = std::thread::spawn(move || {
        crate::map::test_pin(4);
        let mut i = 1;
        while !s.load(Ordering::Relaxed) {
            w.begin(i).unwrap();
            w.write(&[i as u8; 3]).unwrap();
            w.commit_unnotified().unwrap();
            i += 1;
        }
        i
    });
    let seconds = std::env::var("VQA_STRESS_SECONDS")
        .map(|v| v.parse::<u64>().expect("integer stress duration"))
        .unwrap_or(3);
    assert!((1..=60).contains(&seconds));
    let deadline = Instant::now() + Duration::from_secs(seconds);
    let mut observations = 0u64;
    let mut violations = 0u64;
    while Instant::now() < deadline {
        if let Some((seq, ri)) = r.stable_index(u64::MAX) {
            observations += 1;
            if seq / 2 != ri + 1 {
                violations += 1;
            }
        }
    }
    stop.store(true, Ordering::Relaxed);
    let writes = thread.join().unwrap();
    println!("RI_STRESS observations={observations} writes={writes} violations={violations}");
    assert!(observations > 0);
    assert_eq!(
        violations, 0,
        "stable sequence paired with another index generation"
    );
}

#[test]
#[ignore = "run via tools/run_mutants.py with VQA_STRESS=1"]
fn writer_fence_stress() {
    assert!(
        std::env::var_os("VQA_STRESS").is_some(),
        "VQA_STRESS=1 required"
    );
    crate::map::test_pin(3);
    let seconds = std::env::var("VQA_STRESS_SECONDS")
        .map(|v| v.parse::<u64>().expect("integer stress duration"))
        .unwrap_or(3);
    assert!((1..=60).contains(&seconds));
    let g = Geometry::new(1, 32, 1, Format::Rgb24).unwrap();
    let mut w = Writer::create(g, false).unwrap();
    w.publish(0, &[0; 96]).unwrap();
    let reader = w.reader().unwrap();
    let stop = Arc::new(AtomicBool::new(false));
    let s = stop.clone();
    let t = std::thread::spawn(move || {
        crate::map::test_pin(4);
        let mut r = 1;
        let mut payload = [0u8; 96];
        map::hot_loop_audit::start();
        while !s.load(Ordering::Relaxed) {
            payload.fill(r as u8);
            w.begin(r).unwrap();
            w.write(&payload).unwrap();
            w.commit_unnotified().unwrap();
            r += 1;
        }
        map::hot_loop_audit::finish()
    });
    let deadline = Instant::now() + Duration::from_secs(seconds);
    let mut observations = 0;
    let mut violations = 0;
    let mut payload = [0u8; 96];
    map::hot_loop_audit::start();
    while Instant::now() < deadline {
        let seq = reader.map.u64(g.slot(0)).load(Ordering::Acquire);
        if seq == 0 || seq & 1 != 0 { continue; }
        let r = reader.map.u64(g.slot(0)+8).load(Ordering::Relaxed);
        reader.map.load_into(g.payload(0), &mut payload);
        std::sync::atomic::fence(Ordering::Acquire);
        if reader.map.u64(g.slot(0)).load(Ordering::Relaxed) != seq { continue; }
        observations += 1;
        if payload.iter().any(|&v| v != r as u8) { violations += 1; }
    }
    let reader_counts = map::hot_loop_audit::finish();
    stop.store(true, Ordering::Relaxed);
    let writer_counts = t.join().unwrap();
    assert_eq!(reader_counts,(0,0),"reader hot loop heap/futex");
    assert_eq!(writer_counts,(0,0),"writer hot loop heap/futex");
    println!("WRITER_STRESS observations={observations} violations={violations}");
    assert!(observations > 0);
    assert_eq!(violations, 0);
}

#[test]
fn o1_json_conformance() {
    use serde_json::{json, Value};
    use sha2::{Digest, Sha256};
    let mut paths: Vec<_> =
        std::fs::read_dir(concat!(env!("CARGO_MANIFEST_DIR"), "/fixtures/conformance"))
            .unwrap()
            .map(|p| p.unwrap().path())
            .filter(|p| p.extension().is_some_and(|e| e == "json"))
            .collect();
    paths.sort();
    assert!(paths.len() >= 12);
    for path in paths {
        let f: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        let n = |v: &Value| v.as_u64().unwrap();
        let g = &f["geometry"];
        let g = Geometry::new(
            n(&g["slots"]) as u32,
            n(&g["width"]) as u32,
            n(&g["height"]) as u32,
            Format::try_from(n(&g["fmt"]) as u32).unwrap(),
        )
        .unwrap();
        let mut w = Writer::create(g, true).unwrap();
        let mut reader = w.reader().unwrap();
        let seed = n(&f["frames"]["seed"]);
        let mut reads = vec![];
        let record = |r: u64, result: ReadResult| -> Value {
            match result {
                ReadResult::Frame(fr) => {
                    json!({"r":r,"status":"frame","crc32":fr.crc32,"sha256":format!("{:x}",Sha256::digest(&fr.bytes))})
                }
                ReadResult::Absent => json!({"r":r,"status":"absent"}),
                ReadResult::Lapped => json!({"r":r,"status":"lapped"}),
                ReadResult::Torn => json!({"r":r,"status":"torn"}),
            }
        };
        for op in f["ops"].as_array().unwrap() {
            let kind = op["op"].as_str().unwrap();
            match kind {
                "publish" | "begin" => {
                    let r = n(&op["r"]);
                    let data: Vec<_> = (0..g.slot_bytes)
                        .map(|i| (r * 131 + i as u64 * 7 + seed) as u8)
                        .collect();
                    if kind == "publish" {
                        w.publish(r, &data).unwrap()
                    } else {
                        w.begin(r).unwrap();
                        w.write(&data).unwrap()
                    }
                }
                "tear" => {
                    let off = g.payload(n(&op["r"]));
                    w.map.store(off, &[w.map.load(off, 1)[0] ^ 255]);
                }
                "read" => {
                    if op["mode"] == "acquire" {
                        let r = n(&op["r"]);
                        reads.push(record(r, reader.acquire(r, true)));
                    } else {
                        // Observe every attempted read, including drops, then compare
                        // independent fixture state to the production poll operation.
                        let before = reads.len();
                        let h = reader.head();
                        let mut r = reader.last.map_or(0, |v| v + 1);
                        if h > r {
                            if op["mode"] == "latest" {
                                r = h - 1;
                            }
                            r = r.max(h.saturating_sub(g.slots as u64));
                            for i in r..h {
                                reads.push(record(i, reader.try_read(i, true)));
                            }
                        }
                        let delivered = reader.poll(op["mode"] == "latest", true);
                        let expected: Vec<_> = reads[before..]
                            .iter()
                            .filter(|v| v["status"] == "frame")
                            .cloned()
                            .collect();
                        let actual: Vec<_> = delivered
                            .into_iter()
                            .map(|fr| record(fr.render_index, ReadResult::Frame(fr)))
                            .collect();
                        assert_eq!(actual, expected, "poll deliveries");
                    }
                }
                _ => panic!("unknown operation"),
            }
        }
        let mut snapshot = w.map.load(0, g.ring_bytes);
        if g.fmt == Format::Rgb24 {
            snapshot[8..12].copy_from_slice(&2u32.to_le_bytes());
            snapshot[48..64].fill(0);
        } else {
            snapshot[48..52].fill(0);
        }
        let actual = json!({"head":reader.head(),"reads":reads,"overrun":reader.overrun,"torn":reader.torn,"gaps":reader.gaps,"file_sha256":format!("{:x}",Sha256::digest(&snapshot))});
        assert_eq!(actual, f["expect"], "fixture {}", path.display());
    }
}

#[test]
fn v2_polling_compatibility() {
    let g = geometry();
    let mut writer = Writer::create(g, true).unwrap();
    writer.publish(0, &frame(0, g)).unwrap();
    writer.map.store(8, &2u32.to_le_bytes());
    writer.map.store(48, &[0; 16]);
    let reader = writer.reader().unwrap();
    assert_eq!(reader.version, 2);
    let ReadResult::Frame(f) = reader.try_read(0, true) else {
        panic!("v2 read failed")
    };
    assert_eq!(f.bytes, frame(0, g));
    assert!(reader.wait(Duration::from_millis(1)).is_err());
}

#[test]
fn empty_slots_are_absent() {
    for crc in [false, true] {
        let w = Writer::create(geometry(), crc).unwrap();
        let mut reader = w.reader().unwrap();
        for verify in [false, true] {
            for index in [0, 1, 2] {
                assert!(matches!(reader.try_read(index, verify), ReadResult::Absent));
                assert!(matches!(reader.acquire(index, verify), ReadResult::Absent));
            }
        }
        assert_eq!(reader.last, None);
        assert!(reader.torn.is_empty() && reader.gaps.is_empty());
    }
}
#[test]
fn crc_is_stored_at_commit() {
    let g = geometry();
    let mut w = Writer::create(g, true).unwrap();
    w.begin(0).unwrap();
    w.write(&frame(0, g)).unwrap();
    assert_eq!(w.map.u32(g.slot(0) + 16).load(Ordering::Relaxed), 0);
    w.commit().unwrap();
    let old = w.map.u32(g.slot(0) + 16).load(Ordering::Relaxed);
    w.begin(2).unwrap();
    w.write(&frame(2, g)).unwrap();
    assert_eq!(w.map.u32(g.slot(2) + 16).load(Ordering::Relaxed), old);
    w.commit().unwrap();
    assert_eq!(w.map.u32(g.slot(2) + 16).load(Ordering::Relaxed), layout::crc32(&frame(2, g)));
}

#[test]
fn python_v2_byte_identity_and_reader() {
    use serde_json::{json, Value};
    use sha2::{Digest, Sha256};
    let crate_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
    let output = std::process::Command::new("python3")
        .arg(crate_dir.join("tools/python_reference.py")).arg("--live")
        .env("PYTHONDONTWRITEBYTECODE", "1").output().unwrap();
    assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stderr));
    let live: Value = serde_json::from_slice(&output.stdout).unwrap();
    let receipt: Value = serde_json::from_slice(&std::fs::read(crate_dir.join("fixtures/python_v2_receipt.json")).unwrap()).unwrap();
    assert_eq!(live["rtring_sha256"], receipt["rtring_sha256"], "Python source receipt drift");
    let decode = |s: &str| -> Vec<u8> {
        (0..s.len()).step_by(2).map(|i| u8::from_str_radix(&s[i..i+2],16).unwrap()).collect()
    };
    for case in live["cases"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let fixture_bytes = std::fs::read(crate_dir.join(format!("fixtures/python_v2/{name}.json"))).unwrap();
        assert_eq!(format!("{:x}", Sha256::digest(&fixture_bytes)), receipt["fixtures"][format!("{name}.json")].as_str().unwrap());
        let mut persisted = case.clone();
        for phase in persisted["phases"].as_array_mut().unwrap() { phase.as_object_mut().unwrap().remove("bytes"); }
        assert_eq!(persisted, serde_json::from_slice::<Value>(&fixture_bytes).unwrap(), "Python fixture drift {name}");
        let g = Geometry::new(case["geometry"]["slots"].as_u64().unwrap() as u32,4,4,Format::Rgb24).unwrap();
        let mut w = Writer::create(g,true).unwrap();
        w.map.store(8, &2u32.to_le_bytes());
        w.map.store(48, &[0;16]);
        let mut reader = w.reader().unwrap();
        let mut reads = vec![];
        let record = |r: u64, fr: ReadResult| -> Value {
            match fr {
                ReadResult::Frame(f) => json!({"r":r,"status":"frame","crc32":f.crc32,"sha256":format!("{:x}",Sha256::digest(&f.bytes))}),
                ReadResult::Torn => json!({"r":r,"status":"torn"}),
                ReadResult::Absent => json!({"r":r,"status":"absent"}),
                ReadResult::Lapped => json!({"r":r,"status":"lapped"}),
            }
        };
        let compare = |phase: usize, w: &Writer| {
            let mut actual = w.map.load(0,g.ring_bytes);
            actual[48..64].fill(0); // design §3 v3-to-v2 normalization
            let expected = decode(case["phases"][phase]["bytes"].as_str().unwrap());
            assert_eq!(actual[..64], expected[..64], "header {name}/{phase}");
            assert_eq!(actual[64..g.data_off], expected[64..g.data_off], "slot headers {name}/{phase}");
            assert_eq!(actual[g.data_off..], expected[g.data_off..], "payload {name}/{phase}");
        };
        compare(0,&w);
        for (i, op) in case["ops"].as_array().unwrap().iter().enumerate() {
            let r = op["r"].as_u64().unwrap_or(0);
            match op["op"].as_str().unwrap() {
                "publish" => w.publish(r,&frame(r,g)).unwrap(),
                "begin" => { w.begin(r).unwrap(); w.write(&frame(r,g)).unwrap(); },
                "tear" => { let off=g.payload(r); w.map.store(off,&[w.map.load(off,1)[0]^255]); },
                "read" => {
                    if op["mode"] == "acquire" { reads.push(record(r,reader.acquire(r,true))); }
                    else {
                        let frames=reader.poll(op["mode"]=="latest",true);
                        assert!(frames.len()<=1,"v2 poll returns one frame");
                        reads.extend(frames.into_iter().map(|f|record(f.render_index,ReadResult::Frame(f))));
                    }
                },
                _ => panic!("unexpected Python op"),
            }
            compare(i+1,&w);
        }
        let actual=json!({"reads":reads,"last":reader.last.map(|v|v as i64).unwrap_or(-1),"overrun":reader.overrun,"torn":reader.torn,"gaps":reader.gaps});
        assert_eq!(actual,case["expect"],"Python reader state {name}");
    }
}

#[test]
fn reader_index_acquire_structure() {
    let mut command = std::process::Command::new("python3");
    command.arg(concat!(env!("CARGO_MANIFEST_DIR"), "/tools/check_reader_barrier.py"));
    if cfg!(feature="mut-ri-fence") { command.arg("--mutant"); }
    let output = command.env("PYTHONDONTWRITEBYTECODE", "1").output().unwrap();
    println!("{}",String::from_utf8_lossy(&output.stdout));
    assert!(output.status.success(), "{}",String::from_utf8_lossy(&output.stderr));
}

#[test]
fn hot_loop_audit_rejects_legacy_publish_and_copy() {
    let g=geometry();
    let mut writer=Writer::create(g,false).unwrap();
    let reader=writer.reader().unwrap();
    map::hot_loop_audit::start();
    writer.publish(0,&frame(0,g)).unwrap();
    let result=reader.try_read(0,false);
    drop(result);
    let (heap,futex)=map::hot_loop_audit::finish();
    assert!(heap>0 && futex>0,"legacy path must fail hot-loop gate");
}
