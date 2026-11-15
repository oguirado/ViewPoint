// rust/vqa_k_s2_motion/src/main.rs

use std::io::{self, Read, Write};
use std::time::Instant;
use vqa_k_s2_motion::*;

fn read_exact_or_eof(reader: &mut impl Read, buf: &mut [u8]) -> io::Result<bool> {
    let mut read = 0;
    while read < buf.len() {
        match reader.read(&mut buf[read..]) {
            Ok(0) => {
                if read == 0 {
                    return Ok(false);
                } else {
                    return Err(io::Error::new(
                        io::ErrorKind::UnexpectedEof,
                        "unexpected EOF while reading request",
                    ));
                }
            }
            Ok(n) => read += n,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(e),
        }
    }
    Ok(true)
}

fn read_u8(reader: &mut impl Read) -> io::Result<Option<u8>> {
    let mut buf = [0u8; 1];
    if read_exact_or_eof(reader, &mut buf)? {
        Ok(Some(buf[0]))
    } else {
        Ok(None)
    }
}

fn read_u32(reader: &mut impl Read) -> io::Result<u32> {
    let mut buf = [0u8; 4];
    reader.read_exact(&mut buf)?;
    Ok(u32::from_le_bytes(buf))
}

fn read_u64(reader: &mut impl Read) -> io::Result<u64> {
    let mut buf = [0u8; 8];
    reader.read_exact(&mut buf)?;
    Ok(u64::from_le_bytes(buf))
}

fn read_i64(reader: &mut impl Read) -> io::Result<i64> {
    let mut buf = [0u8; 8];
    reader.read_exact(&mut buf)?;
    Ok(i64::from_le_bytes(buf))
}

struct Request {
    width: usize,
    height: usize,
    s: usize,
    y_cur: Vec<i16>,
    y_prev: Vec<i16>,
    mask: Vec<bool>,
    gh: usize,
    gw: usize,
}

fn read_request(reader: &mut impl Read) -> io::Result<Option<Request>> {
    let width_i = match read_i64(reader) {
        Ok(v) => v,
        Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => return Ok(None),
        Err(e) => return Err(e),
    };
    let height_i = read_i64(reader)?;
    let s_i = read_i64(reader)?;

    if width_i < 0 || height_i < 0 || s_i <= 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "invalid dimensions or tile size",
        ));
    }

    let width = width_i as usize;
    let height = height_i as usize;
    let s = s_i as usize;

    let len_cur = read_u64(reader)? as usize;
    let len_prev = read_u64(reader)? as usize;

    let expected_px = width * height;
    if len_cur < expected_px * 2 || len_prev < expected_px * 2 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "luma buffer too short",
        ));
    }

    let mut cur_bytes = vec![0u8; len_cur];
    reader.read_exact(&mut cur_bytes)?;
    let mut prev_bytes = vec![0u8; len_prev];
    reader.read_exact(&mut prev_bytes)?;

    let mut y_cur = vec![0i16; expected_px];
    for i in 0..expected_px {
        let b = [cur_bytes[i * 2], cur_bytes[i * 2 + 1]];
        y_cur[i] = i16::from_le_bytes(b);
    }

    let mut y_prev = vec![0i16; expected_px];
    for i in 0..expected_px {
        let b = [prev_bytes[i * 2], prev_bytes[i * 2 + 1]];
        y_prev[i] = i16::from_le_bytes(b);
    }

    let gw = width / s;
    let gh = height / s;
    let n_tiles = read_u64(reader)? as usize;
    if n_tiles < gh * gw {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "mask tile count too short",
        ));
    }

    let mut mask_bytes = vec![0u8; n_tiles];
    reader.read_exact(&mut mask_bytes)?;
    let mask: Vec<bool> = mask_bytes[..gh * gw].iter().map(|&b| b != 0).collect();

    Ok(Some(Request {
        width,
        height,
        s,
        y_cur,
        y_prev,
        mask,
        gh,
        gw,
    }))
}

fn execute_request(req: &Request) -> (i32, i32, i32, Vec<i32>, i32, i32, i32) {
    let (gdx, gdy) = global_vector(&req.y_cur, &req.y_prev, req.width, req.height, req.s);
    let (lab, n_islands) = islands(&req.mask, req.gh, req.gw);
    let (idx, idy) = island_vector(
        &req.mask,
        &req.y_cur,
        &req.y_prev,
        req.width,
        req.height,
        req.s,
        req.gh,
        req.gw,
    );
    (0, gdx, gdy, lab, n_islands, idx, idy)
}

fn write_response(writer: &mut impl Write, req: &Request) -> io::Result<()> {
    let (status, gdx, gdy, lab, n_islands, idx, idy) = execute_request(req);
    writer.write_all(&status.to_le_bytes())?;
    writer.write_all(&gdx.to_le_bytes())?;
    writer.write_all(&gdy.to_le_bytes())?;

    writer.write_all(&(lab.len() as u64).to_le_bytes())?;
    for &l in &lab {
        writer.write_all(&l.to_le_bytes())?;
    }

    writer.write_all(&n_islands.to_le_bytes())?;
    writer.write_all(&idx.to_le_bytes())?;
    writer.write_all(&idy.to_le_bytes())?;
    writer.flush()?;
    Ok(())
}

fn main() -> io::Result<()> {
    let mut stdin = io::stdin().lock();
    let mut stdout = io::stdout().lock();

    loop {
        let op = match read_u8(&mut stdin)? {
            Some(v) => v,
            None => break,
        };

        match op {
            0 => {
                // Quit
                break;
            }
            1 => {
                // Compute one request
                if let Some(req) = read_request(&mut stdin)? {
                    write_response(&mut stdout, &req)?;
                } else {
                    break;
                }
            }
            3 => {
                // Bench request
                if let Some(req) = read_request(&mut stdin)? {
                    let warmup = read_u32(&mut stdin)?;
                    let iters = read_u32(&mut stdin)?;

                    for _ in 0..warmup {
                        let _ = execute_request(&req);
                    }

                    let mut times_ns = Vec::with_capacity(iters as usize);
                    for _ in 0..iters {
                        let start = Instant::now();
                        let _ = execute_request(&req);
                        let elapsed = start.elapsed().as_nanos() as u64;
                        times_ns.push(elapsed);
                    }

                    stdout.write_all(&(iters as u32).to_le_bytes())?;
                    for t in times_ns {
                        stdout.write_all(&t.to_le_bytes())?;
                    }
                    stdout.flush()?;
                } else {
                    break;
                }
            }
            _ => {
                eprintln!("Unknown op: {}", op);
                break;
            }
        }
    }

    Ok(())
}
