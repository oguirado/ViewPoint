use std::io::{self, BufReader, BufWriter, Read, Write};
use vqa_k_s1_desc::{
    compute_all_tiles, pad_frame, Grid, TileDesc, TAU_EDGE,
};

fn read_u8<R: Read>(r: &mut R) -> io::Result<u8> {
    let mut buf = [0u8; 1];
    r.read_exact(&mut buf)?;
    Ok(buf[0])
}

fn read_u32<R: Read>(r: &mut R) -> io::Result<u32> {
    let mut buf = [0u8; 4];
    r.read_exact(&mut buf)?;
    Ok(u32::from_le_bytes(buf))
}

struct Request {
    w: u32,
    h: u32,
    requested_tiles: Vec<usize>,
    frame_bytes: Vec<u8>,
    _prev_bytes: Vec<u8>,
}

fn read_request<R: Read>(r: &mut R) -> io::Result<Request> {
    let w = read_u32(r)?;
    let h = read_u32(r)?;
    let n_tiles = read_u32(r)? as usize;
    let mut requested_tiles = Vec::with_capacity(n_tiles);
    for _ in 0..n_tiles {
        requested_tiles.push(read_u32(r)? as usize);
    }
    let frame_len = read_u32(r)? as usize;
    let mut frame_bytes = vec![0u8; frame_len];
    r.read_exact(&mut frame_bytes)?;

    let prev_len = read_u32(r)? as usize;
    let mut prev_bytes = vec![0u8; prev_len];
    if prev_len > 0 {
        r.read_exact(&mut prev_bytes)?;
    }

    Ok(Request {
        w,
        h,
        requested_tiles,
        frame_bytes,
        _prev_bytes: prev_bytes,
    })
}

fn process_request(req: &Request) -> Result<Vec<TileDesc>, i32> {
    let grid = match Grid::new(req.w, req.h) {
        Ok(g) => g,
        Err(_) => return Err(1),
    };

    let tiles_to_run = if req.requested_tiles.is_empty() {
        (0..grid.n).collect()
    } else {
        req.requested_tiles.clone()
    };

    for &rank in &tiles_to_run {
        if rank >= grid.n {
            return Err(2);
        }
    }

    let padded_w = (grid.gw * grid.s) as usize;
    let padded_h = (grid.gh * grid.s) as usize;
    let expected_raw_len = (req.w * req.h * 3) as usize;
    let expected_padded_len = padded_w * padded_h * 3;

    let padded = if req.frame_bytes.len() == expected_padded_len {
        req.frame_bytes.clone()
    } else if req.frame_bytes.len() == expected_raw_len {
        pad_frame(&req.frame_bytes, req.w, req.h, &grid)
    } else {
        return Err(3);
    };

    let descs = compute_all_tiles(&padded, &grid, &tiles_to_run, TAU_EDGE);
    Ok(descs)
}

fn main() -> io::Result<()> {
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut reader = BufReader::new(stdin.lock());
    let mut writer = BufWriter::new(stdout.lock());

    loop {
        let op = match read_u8(&mut reader) {
            Ok(op) => op,
            Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => break,
            Err(e) => return Err(e),
        };

        match op {
            0 => break, // Quit
            1 => {
                // Compute request
                let req = read_request(&mut reader)?;
                match process_request(&req) {
                    Ok(descs) => {
                        writer.write_all(&0i32.to_le_bytes())?;
                        writer.write_all(&(descs.len() as u32).to_le_bytes())?;
                        writer.write_all(&((descs.len() * 32) as u32).to_le_bytes())?;
                        for desc in &descs {
                            writer.write_all(&desc.to_bytes())?;
                        }
                        writer.flush()?;
                    }
                    Err(status) => {
                        writer.write_all(&status.to_le_bytes())?;
                        writer.write_all(&0u32.to_le_bytes())?;
                        writer.write_all(&0u32.to_le_bytes())?;
                        writer.flush()?;
                    }
                }
            }
            3 => {
                // Bench request
                let req = read_request(&mut reader)?;
                let warmup = read_u32(&mut reader)?;
                let iters = read_u32(&mut reader)?;

                let grid = match Grid::new(req.w, req.h) {
                    Ok(g) => g,
                    Err(_) => {
                        writer.write_all(&0u32.to_le_bytes())?;
                        writer.flush()?;
                        continue;
                    }
                };

                let tiles_to_run = if req.requested_tiles.is_empty() {
                    (0..grid.n).collect()
                } else {
                    req.requested_tiles.clone()
                };

                let padded_w = (grid.gw * grid.s) as usize;
                let padded_h = (grid.gh * grid.s) as usize;
                let expected_raw_len = (req.w * req.h * 3) as usize;
                let expected_padded_len = padded_w * padded_h * 3;

                let padded = if req.frame_bytes.len() == expected_padded_len {
                    req.frame_bytes.clone()
                } else if req.frame_bytes.len() == expected_raw_len {
                    pad_frame(&req.frame_bytes, req.w, req.h, &grid)
                } else {
                    writer.write_all(&0u32.to_le_bytes())?;
                    writer.flush()?;
                    continue;
                };

                for _ in 0..warmup {
                    let _ = compute_all_tiles(&padded, &grid, &tiles_to_run, TAU_EDGE);
                }

                let mut dur_ns = Vec::with_capacity(iters as usize);
                for _ in 0..iters {
                    let start = std::time::Instant::now();
                    let _ = compute_all_tiles(&padded, &grid, &tiles_to_run, TAU_EDGE);
                    let elapsed = start.elapsed().as_nanos() as u64;
                    dur_ns.push(elapsed);
                }

                writer.write_all(&(iters as u32).to_le_bytes())?;
                for ns in dur_ns {
                    writer.write_all(&ns.to_le_bytes())?;
                }
                writer.flush()?;
            }
            _ => {
                eprintln!("Unknown op: {}", op);
                break;
            }
        }
    }

    Ok(())
}
