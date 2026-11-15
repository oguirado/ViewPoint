//! Binary CLI for vqa_k_st_block4 speaking little-endian stdin/stdout protocol.
use std::io::{self, BufReader, BufWriter, Read, Write};
use std::time::Instant;
use vqa_k_st_block4::{subtile_motion_batch, SubtileResult};

struct Io {
    r: BufReader<io::Stdin>,
    w: BufWriter<io::Stdout>,
}

impl Io {
    fn new() -> Self {
        Self {
            r: BufReader::with_capacity(1 << 20, io::stdin()),
            w: BufWriter::with_capacity(1 << 20, io::stdout()),
        }
    }

    fn read_exact_bytes(&mut self, n: usize) -> Option<Vec<u8>> {
        let mut buf = vec![0u8; n];
        if self.r.read_exact(&mut buf).is_err() {
            None
        } else {
            Some(buf)
        }
    }

    fn u8(&mut self) -> Option<u8> {
        self.read_exact_bytes(1).map(|b| b[0])
    }

    fn i32(&mut self) -> Option<i32> {
        self.read_exact_bytes(4)
            .map(|b| i32::from_le_bytes(b.try_into().unwrap()))
    }

    fn u32(&mut self) -> Option<u32> {
        self.read_exact_bytes(4)
            .map(|b| u32::from_le_bytes(b.try_into().unwrap()))
    }

    fn u64(&mut self) -> Option<u64> {
        self.read_exact_bytes(8)
            .map(|b| u64::from_le_bytes(b.try_into().unwrap()))
    }

    fn wi32(&mut self, v: i32) {
        self.w.write_all(&v.to_le_bytes()).unwrap();
    }

    fn wu32(&mut self, v: u32) {
        self.w.write_all(&v.to_le_bytes()).unwrap();
    }

    fn wu64(&mut self, v: u64) {
        self.w.write_all(&v.to_le_bytes()).unwrap();
    }

    fn flush(&mut self) {
        let _ = self.w.flush();
    }
}

struct Request {
    w: i32,
    h: i32,
    s: i32,
    max_d: i32,
    tiles: Vec<(i32, i32)>,
    prev_luma: Vec<u8>,
    cur_luma: Vec<u8>,
}

fn read_request(io: &mut Io) -> Option<Request> {
    let w = io.i32()?;
    let h = io.i32()?;
    let s = io.i32()?;
    let max_d = io.i32()?;
    let n_tiles = io.u32()? as usize;

    let mut tiles = Vec::with_capacity(n_tiles);
    for _ in 0..n_tiles {
        let tx = io.i32()?;
        let ty = io.i32()?;
        tiles.push((tx, ty));
    }

    let frame_bytes = io.u64()? as usize;
    let prev_luma = io.read_exact_bytes(frame_bytes)?;
    let cur_luma = io.read_exact_bytes(frame_bytes)?;

    Some(Request {
        w,
        h,
        s,
        max_d,
        tiles,
        prev_luma,
        cur_luma,
    })
}

fn write_results(io: &mut Io, results: &[Option<SubtileResult>]) {
    io.wi32(0); // status 0 = success
    io.wu32(results.len() as u32);
    for res in results {
        match res {
            Some(r) => {
                io.wi32(r.dx);
                io.wi32(r.dy);
                io.wi32(r.n_blocks);
                io.wi32(1); // valid = 1
            }
            None => {
                io.wi32(0);
                io.wi32(0);
                io.wi32(0);
                io.wi32(0); // valid = 0
            }
        }
    }
}

fn main() {
    let mut io = Io::new();

    loop {
        let op = match io.u8() {
            Some(op) => op,
            None => break,
        };

        match op {
            0 => break, // quit
            1 => {
                let req = match read_request(&mut io) {
                    Some(r) => r,
                    None => break,
                };
                let results = subtile_motion_batch(
                    &req.prev_luma,
                    &req.cur_luma,
                    req.w,
                    req.h,
                    req.s,
                    req.max_d,
                    &req.tiles,
                );
                write_results(&mut io, &results);
                io.flush();
            }
            3 => {
                let req = match read_request(&mut io) {
                    Some(r) => r,
                    None => break,
                };
                let warmup = match io.u32() {
                    Some(w) => w,
                    None => break,
                };
                let iters = match io.u32() {
                    Some(i) => i,
                    None => break,
                };

                for _ in 0..warmup {
                    let _ = std::hint::black_box(subtile_motion_batch(
                        &req.prev_luma,
                        &req.cur_luma,
                        req.w,
                        req.h,
                        req.s,
                        req.max_d,
                        &req.tiles,
                    ));
                }

                io.wu32(iters);
                for _ in 0..iters {
                    let t0 = Instant::now();
                    let res = subtile_motion_batch(
                        std::hint::black_box(&req.prev_luma),
                        std::hint::black_box(&req.cur_luma),
                        req.w,
                        req.h,
                        req.s,
                        req.max_d,
                        std::hint::black_box(&req.tiles),
                    );
                    std::hint::black_box(&res);
                    io.wu64(t0.elapsed().as_nanos() as u64);
                }
                io.flush();
            }
            _ => {
                eprintln!("Unknown op {}", op);
                std::process::exit(2);
            }
        }
    }
}
