use std::io::{self, Read, Write};
use std::time::Instant;
use vqa_k_gm_proto::{consensus_tiles, estimate_global_motion, grid_params};

fn read_exact(reader: &mut impl Read, buf: &mut [u8]) -> io::Result<()> {
    reader.read_exact(buf)
}

fn read_u32_le(reader: &mut impl Read) -> io::Result<u32> {
    let mut buf = [0u8; 4];
    read_exact(reader, &mut buf)?;
    Ok(u32::from_le_bytes(buf))
}

fn read_i32_le(reader: &mut impl Read) -> io::Result<i32> {
    let mut buf = [0u8; 4];
    read_exact(reader, &mut buf)?;
    Ok(i32::from_le_bytes(buf))
}

fn write_all(writer: &mut impl Write, buf: &[u8]) -> io::Result<()> {
    writer.write_all(buf)
}

fn write_i32_le(writer: &mut impl Write, val: i32) -> io::Result<()> {
    write_all(writer, &val.to_le_bytes())
}

fn write_u32_le(writer: &mut impl Write, val: u32) -> io::Result<()> {
    write_all(writer, &val.to_le_bytes())
}

fn write_u64_le(writer: &mut impl Write, val: u64) -> io::Result<()> {
    write_all(writer, &val.to_le_bytes())
}

fn write_i64_le(writer: &mut impl Write, val: i64) -> io::Result<()> {
    write_all(writer, &val.to_le_bytes())
}

fn handle_compute(stdin: &mut io::StdinLock, stdout: &mut io::StdoutLock) -> io::Result<()> {
    let h = read_u32_le(stdin)? as usize;
    let w = read_u32_le(stdin)? as usize;
    let max_d = read_i32_le(stdin)?;
    let k_top = read_u32_le(stdin)? as usize;
    let s = read_u32_le(stdin)? as usize;

    let len = h * w;
    let mut prev = vec![0u8; len];
    let mut cur = vec![0u8; len];

    read_exact(stdin, &mut prev)?;
    read_exact(stdin, &mut cur)?;

    let gm_res = estimate_global_motion(&prev, &cur, h, w, max_d, k_top);
    let (has_gm, dx, dy, score) = match gm_res {
        Some((dx, dy, score)) => (1u8, dx, dy, score),
        None => (0u8, 0i32, 0i32, 0i64),
    };

    let (_grid_s, gw, gh) = grid_params(w, h);
    let mask = consensus_tiles(&prev, &cur, h, w, s, dx, dy);

    write_i32_le(stdout, 0)?; // status 0 OK
    write_all(stdout, &[has_gm])?;
    write_i32_le(stdout, dx)?;
    write_i32_le(stdout, dy)?;
    write_i64_le(stdout, score)?;
    write_u32_le(stdout, gh as u32)?;
    write_u32_le(stdout, gw as u32)?;

    let mask_bytes: Vec<u8> = mask.iter().map(|&b| if b { 1u8 } else { 0u8 }).collect();
    assert_eq!(mask_bytes.len(), gh * gw);
    write_all(stdout, &mask_bytes)?;
    stdout.flush()?;

    Ok(())
}

fn handle_consensus_explicit(stdin: &mut io::StdinLock, stdout: &mut io::StdoutLock) -> io::Result<()> {
    let h = read_u32_le(stdin)? as usize;
    let w = read_u32_le(stdin)? as usize;
    let s = read_u32_le(stdin)? as usize;
    let dx = read_i32_le(stdin)?;
    let dy = read_i32_le(stdin)?;

    let len = h * w;
    let mut prev = vec![0u8; len];
    let mut cur = vec![0u8; len];

    read_exact(stdin, &mut prev)?;
    read_exact(stdin, &mut cur)?;

    let (_grid_s, gw, gh) = grid_params(w, h);
    let mask = consensus_tiles(&prev, &cur, h, w, s, dx, dy);

    write_i32_le(stdout, 0)?; // status 0 OK
    write_u32_le(stdout, gh as u32)?;
    write_u32_le(stdout, gw as u32)?;

    let mask_bytes: Vec<u8> = mask.iter().map(|&b| if b { 1u8 } else { 0u8 }).collect();
    assert_eq!(mask_bytes.len(), gh * gw);
    write_all(stdout, &mask_bytes)?;
    stdout.flush()?;

    Ok(())
}

fn handle_bench(stdin: &mut io::StdinLock, stdout: &mut io::StdoutLock) -> io::Result<()> {
    let h = read_u32_le(stdin)? as usize;
    let w = read_u32_le(stdin)? as usize;
    let max_d = read_i32_le(stdin)?;
    let k_top = read_u32_le(stdin)? as usize;
    let s = read_u32_le(stdin)? as usize;

    let len = h * w;
    let mut prev = vec![0u8; len];
    let mut cur = vec![0u8; len];

    read_exact(stdin, &mut prev)?;
    read_exact(stdin, &mut cur)?;

    let warmup = read_u32_le(stdin)?;
    let iters = read_u32_le(stdin)?;

    for _ in 0..warmup {
        let gm_res = estimate_global_motion(&prev, &cur, h, w, max_d, k_top);
        let (dx, dy) = match gm_res {
            Some((dx, dy, _)) => (dx, dy),
            None => (0, 0),
        };
        let _mask = consensus_tiles(&prev, &cur, h, w, s, dx, dy);
    }

    let mut timings = Vec::with_capacity(iters as usize);
    for _ in 0..iters {
        let start = Instant::now();
        let gm_res = estimate_global_motion(&prev, &cur, h, w, max_d, k_top);
        let (dx, dy) = match gm_res {
            Some((dx, dy, _)) => (dx, dy),
            None => (0, 0),
        };
        let _mask = consensus_tiles(&prev, &cur, h, w, s, dx, dy);
        let elapsed = start.elapsed().as_nanos() as u64;
        timings.push(elapsed);
    }

    write_u32_le(stdout, iters)?;
    for t in timings {
        write_u64_le(stdout, t)?;
    }
    stdout.flush()?;

    Ok(())
}

fn main() -> io::Result<()> {
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut stdin_lock = stdin.lock();
    let mut stdout_lock = stdout.lock();

    loop {
        let mut op_buf = [0u8; 4];
        if stdin_lock.read_exact(&mut op_buf).is_err() {
            break;
        }
        let op = i32::from_le_bytes(op_buf);
        match op {
            0 => break, // op 0 quit
            1 => handle_compute(&mut stdin_lock, &mut stdout_lock)?,
            2 => handle_consensus_explicit(&mut stdin_lock, &mut stdout_lock)?,
            3 => handle_bench(&mut stdin_lock, &mut stdout_lock)?,
            _ => {
                eprintln!("Unknown op {}", op);
                break;
            }
        }
    }

    Ok(())
}
