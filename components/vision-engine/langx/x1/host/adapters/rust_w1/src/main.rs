//! Thin LANG-X X1 host adapter. Depends on the seed crate by path (read-only)
//! and speaks the binary stdin/stdout protocol documented in
//! langx/x1/host/README.md. Generic over the seed's integer parameter types
//! via `as _` / `TryFrom`; error variants are mapped from their Debug names.
use std::io::{self, BufReader, BufWriter, Read, Write};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::time::Instant;

/// true when the seed takes w/h/s as signed integers (negative values representable).
const SIGNED_DIMS: bool = true;
const ST_OK: i32 = 0;
const ST_PANIC: i32 = 97;
const ST_OTHER: i32 = 98;
const ST_NA: i32 = 99; // value not representable in the seed's unsigned parameter type

struct Io {
    r: BufReader<io::Stdin>,
    w: BufWriter<io::Stdout>,
}
impl Io {
    fn rd(&mut self, n: usize) -> Vec<u8> {
        let mut b = vec![0u8; n];
        if self.r.read_exact(&mut b).is_err() {
            let _ = self.w.flush();
            std::process::exit(0);
        }
        b
    }
    fn i64(&mut self) -> i64 { i64::from_le_bytes(self.rd(8).try_into().unwrap()) }
    fn u64(&mut self) -> u64 { u64::from_le_bytes(self.rd(8).try_into().unwrap()) }
    fn u32(&mut self) -> u32 { u32::from_le_bytes(self.rd(4).try_into().unwrap()) }
    fn i32(&mut self) -> i32 { i32::from_le_bytes(self.rd(4).try_into().unwrap()) }
    fn wi32(&mut self, v: i32) { self.w.write_all(&v.to_le_bytes()).unwrap(); }
    fn wu32(&mut self, v: u32) { self.w.write_all(&v.to_le_bytes()).unwrap(); }
    fn wu64(&mut self, v: u64) { self.w.write_all(&v.to_le_bytes()).unwrap(); }
    fn status(&mut self, st: i32, msg: &str) {
        self.wi32(st);
        if st == ST_OTHER || st == ST_PANIC {
            self.wu32(msg.len() as u32);
            self.w.write_all(msg.as_bytes()).unwrap();
        }
    }
}

fn map_err<E: std::fmt::Debug>(e: E) -> (i32, String) {
    let name = format!("{:?}", e);
    let n = name.strip_prefix('E').filter(|s| s.chars().next().map_or(false, |c| c.is_uppercase())).unwrap_or(&name);
    match n.to_ascii_lowercase().as_str() {
        "dim" => (1, String::new()),
        "stride" => (2, String::new()),
        "short" => (3, String::new()),
        "rect" => (4, String::new()),
        "grid" => (5, String::new()),
        _ => (ST_OTHER, name),
    }
}

fn panic_msg(p: Box<dyn std::any::Any + Send>) -> String {
    if let Some(s) = p.downcast_ref::<&str>() { s.to_string() }
    else if let Some(s) = p.downcast_ref::<String>() { s.clone() }
    else { "panic".to_string() }
}

#[allow(clippy::type_complexity)]
fn read_k1(io: &mut Io) -> (i64, i64, i64, i64, Vec<u8>, Vec<u8>) {
    let (w, h, ss, ds) = (io.i64(), io.i64(), io.i64(), io.i64());
    let (sl, dl) = (io.u64() as usize, io.u64() as usize);
    let src = io.rd(sl);
    let dst = io.rd(dl);
    (w, h, ss, ds, src, dst)
}

fn read_k2(io: &mut Io) -> (i64, i64, i64, Vec<(i32, i32, i32, i32)>) {
    let (w, h, s) = (io.i64(), io.i64(), io.i64());
    let n = io.u64() as usize;
    let rects = (0..n).map(|_| (io.i32(), io.i32(), io.i32(), io.i32())).collect();
    (w, h, s, rects)
}

fn k1(w: i64, h: i64, ss: i64, ds: i64, src: &[u8], dst: &mut [u8]) -> (i32, String) {
    if (!SIGNED_DIMS && (w < 0 || h < 0)) || ss < 0 || ds < 0 {
        return (ST_NA, String::new());
    }
    match catch_unwind(AssertUnwindSafe(|| {
        seed::repack_bgrx_to_rgb(src, w as _, h as _, ss as usize, dst, ds as usize)
    })) {
        Ok(Ok(())) => (ST_OK, String::new()),
        Ok(Err(e)) => map_err(e),
        Err(p) => (ST_PANIC, panic_msg(p)),
    }
}

fn k2(w: i64, h: i64, s: i64, rects: &[(i32, i32, i32, i32)]) -> (i32, String, u64, Vec<u64>) {
    if !SIGNED_DIMS && (w < 0 || h < 0 || s < 0) {
        return (ST_NA, String::new(), 0, Vec::new());
    }
    match catch_unwind(AssertUnwindSafe(|| seed::damage_tiles(w as _, h as _, s as _, rects))) {
        Ok(Ok((words, pc))) => (ST_OK, String::new(), pc as u64, words),
        Ok(Err(e)) => { let (c, m) = map_err(e); (c, m, 0, Vec::new()) }
        Err(p) => (ST_PANIC, panic_msg(p), 0, Vec::new()),
    }
}

fn main() {
    let mut io = Io { r: BufReader::with_capacity(1 << 20, io::stdin()), w: BufWriter::with_capacity(1 << 20, io::stdout()) };
    loop {
        let op = io.rd(1)[0];
        match op {
            0 => break,
            1 => {
                let (w, h, ss, ds, src, mut dst) = read_k1(&mut io);
                let (st, msg) = k1(w, h, ss, ds, &src, &mut dst);
                io.status(st, &msg);
                io.wu64(dst.len() as u64);
                io.w.write_all(&dst).unwrap();
            }
            2 => {
                let (w, h, s, rects) = read_k2(&mut io);
                let (st, msg, pc, words) = k2(w, h, s, &rects);
                io.status(st, &msg);
                io.wu64(pc);
                io.wu64(words.len() as u64);
                for v in words { io.wu64(v); }
            }
            3 => {
                let (w, h, ss, ds, src, mut dst) = read_k1(&mut io);
                let (warm, iters) = (io.u32(), io.u32());
                for _ in 0..warm {
                    let _ = seed::repack_bgrx_to_rgb(&src, w as _, h as _, ss as usize, &mut dst, ds as usize);
                }
                io.wu32(iters);
                for _ in 0..iters {
                    let t0 = Instant::now();
                    let r = seed::repack_bgrx_to_rgb(std::hint::black_box(&src), w as _, h as _, ss as usize, &mut dst, ds as usize);
                    std::hint::black_box(&r);
                    std::hint::black_box(&mut dst);
                    io.wu64(t0.elapsed().as_nanos() as u64);
                }
            }
            4 => {
                let (w, h, s, rects) = read_k2(&mut io);
                let (warm, iters) = (io.u32(), io.u32());
                for _ in 0..warm {
                    let _ = std::hint::black_box(seed::damage_tiles(w as _, h as _, s as _, &rects));
                }
                io.wu32(iters);
                for _ in 0..iters {
                    let t0 = Instant::now();
                    let r = seed::damage_tiles(w as _, h as _, s as _, std::hint::black_box(&rects));
                    std::hint::black_box(&r);
                    io.wu64(t0.elapsed().as_nanos() as u64);
                }
            }
            _ => { eprintln!("bad op {}", op); std::process::exit(2); }
        }
        io.w.flush().unwrap();
    }
    io.w.flush().unwrap();
}
