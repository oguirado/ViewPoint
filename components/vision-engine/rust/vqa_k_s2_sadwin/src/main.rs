use std::io::{self, BufReader, BufWriter, Read, Write};
use std::time::Instant;
use vqa_k_s2_sadwin::{tile_sad_windows, SadWinError};

const ST_OK: i32 = 0;
const ST_INDEX_OUT_OF_BOUNDS: i32 = 1;
const ST_DIM_MISMATCH: i32 = 2;
const ST_INVALID_TILE_SIZE: i32 = 3;
const ST_OTHER: i32 = 98;

struct Io<R: Read, W: Write> {
    r: BufReader<R>,
    w: BufWriter<W>,
}

impl<R: Read, W: Write> Io<R, W> {
    fn new(r: R, w: W) -> Self {
        Self {
            r: BufReader::new(r),
            w: BufWriter::new(w),
        }
    }

    fn rd(&mut self, n: usize) -> io::Result<Vec<u8>> {
        let mut b = vec![0u8; n];
        self.r.read_exact(&mut b)?;
        Ok(b)
    }

    fn u8(&mut self) -> io::Result<u8> {
        let b = self.rd(1)?;
        Ok(b[0])
    }

    fn u32(&mut self) -> io::Result<u32> {
        let b = self.rd(4)?;
        Ok(u32::from_le_bytes(b.try_into().unwrap()))
    }

    fn i64(&mut self) -> io::Result<i64> {
        let b = self.rd(8)?;
        Ok(i64::from_le_bytes(b.try_into().unwrap()))
    }

    fn u64(&mut self) -> io::Result<u64> {
        let b = self.rd(8)?;
        Ok(u64::from_le_bytes(b.try_into().unwrap()))
    }

    fn wi32(&mut self, v: i32) -> io::Result<()> {
        self.w.write_all(&v.to_le_bytes())
    }

    fn wu32(&mut self, v: u32) -> io::Result<()> {
        self.w.write_all(&v.to_le_bytes())
    }

    fn wu64(&mut self, v: u64) -> io::Result<()> {
        self.w.write_all(&v.to_le_bytes())
    }

    fn flush(&mut self) -> io::Result<()> {
        self.w.flush()
    }
}

struct Request {
    h: usize,
    w: usize,
    dx: i64,
    dy: i64,
    s: usize,
    y_cur: Vec<i16>,
    y_prev: Vec<i16>,
    tx: Vec<i64>,
    ty: Vec<i64>,
}

fn read_request<R: Read, W: Write>(io: &mut Io<R, W>) -> io::Result<Request> {
    let h = io.i64()? as usize;
    let w = io.i64()? as usize;
    let dx = io.i64()?;
    let dy = io.i64()?;
    let s = io.i64()? as usize;
    let len_pixels = io.u64()? as usize;
    let n_req = io.u64()? as usize;

    let mut y_cur = Vec::with_capacity(len_pixels);
    for _ in 0..len_pixels {
        y_cur.push(io.i64()? as i16);
    }

    let mut y_prev = Vec::with_capacity(len_pixels);
    for _ in 0..len_pixels {
        y_prev.push(io.i64()? as i16);
    }

    let mut tx = Vec::with_capacity(n_req);
    for _ in 0..n_req {
        tx.push(io.i64()?);
    }

    let mut ty = Vec::with_capacity(n_req);
    for _ in 0..n_req {
        ty.push(io.i64()?);
    }

    Ok(Request {
        h,
        w,
        dx,
        dy,
        s,
        y_cur,
        y_prev,
        tx,
        ty,
    })
}

fn write_response<R: Read, W: Write>(
    io: &mut Io<R, W>,
    res: std::result::Result<(Vec<i64>, Vec<i64>), SadWinError>,
) -> io::Result<()> {
    match res {
        Ok((sad, count)) => {
            io.wi32(ST_OK)?;
            io.wu64(sad.len() as u64)?;
            for val in sad {
                io.wu64(val as u64)?;
            }
            io.wu64(count.len() as u64)?;
            for val in count {
                io.wu64(val as u64)?;
            }
        }
        Err(e) => {
            let st = match e {
                SadWinError::IndexOutOfBounds => ST_INDEX_OUT_OF_BOUNDS,
                SadWinError::DimensionMismatch => ST_DIM_MISMATCH,
                SadWinError::InvalidTileSize => ST_INVALID_TILE_SIZE,
            };
            io.wi32(st)?;
        }
    }
    io.flush()
}

fn main() -> io::Result<()> {
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut io = Io::new(stdin.lock(), stdout.lock());

    loop {
        let op = match io.u8() {
            Ok(op) => op,
            Err(_) => break, // EOF
        };

        match op {
            0 => {
                // quit
                break;
            }
            1 => {
                // compute
                let req = read_request(&mut io)?;
                let res = tile_sad_windows(
                    &req.y_cur,
                    &req.y_prev,
                    req.h,
                    req.w,
                    req.dx,
                    req.dy,
                    req.s,
                    &req.tx,
                    &req.ty,
                );
                write_response(&mut io, res)?;
            }
            3 => {
                // bench
                let req = read_request(&mut io)?;
                let warmup = io.u32()?;
                let iters = io.u32()?;

                for _ in 0..warmup {
                    let _ = tile_sad_windows(
                        &req.y_cur,
                        &req.y_prev,
                        req.h,
                        req.w,
                        req.dx,
                        req.dy,
                        req.s,
                        &req.tx,
                        &req.ty,
                    );
                }

                let mut times = Vec::with_capacity(iters as usize);
                for _ in 0..iters {
                    let t0 = Instant::now();
                    let _ = tile_sad_windows(
                        &req.y_cur,
                        &req.y_prev,
                        req.h,
                        req.w,
                        req.dx,
                        req.dy,
                        req.s,
                        &req.tx,
                        &req.ty,
                    );
                    let elapsed = t0.elapsed().as_nanos() as u64;
                    times.push(elapsed);
                }

                io.wu32(iters)?;
                for ns in times {
                    io.wu64(ns)?;
                }
                io.flush()?;
            }
            _ => {
                io.wi32(ST_OTHER)?;
                io.flush()?;
                break;
            }
        }
    }

    Ok(())
}
