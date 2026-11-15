use std::io::{self, Read, Write};
use std::time::Instant;
use vqa_k_st_proto8::subtile_motion;

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

fn read_i32<R: Read>(r: &mut R) -> io::Result<i32> {
    let mut buf = [0u8; 4];
    r.read_exact(&mut buf)?;
    Ok(i32::from_le_bytes(buf))
}

fn read_exact_vec<R: Read>(r: &mut R, len: usize) -> io::Result<Vec<u8>> {
    let mut vec = vec![0u8; len];
    r.read_exact(&mut vec)?;
    Ok(vec)
}

fn main() -> io::Result<()> {
    let stdin = io::stdin();
    let mut stdin_lock = stdin.lock();
    let stdout = io::stdout();
    let mut stdout_lock = stdout.lock();

    loop {
        let op = match read_u8(&mut stdin_lock) {
            Ok(op) => op,
            Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => break,
            Err(e) => return Err(e),
        };

        if op == 0 {
            break;
        }

        if op == 1 || op == 3 {
            let w = read_u32(&mut stdin_lock)?;
            let h = read_u32(&mut stdin_lock)?;
            let s = read_u32(&mut stdin_lock)?;
            let max_d = read_i32(&mut stdin_lock)?;

            let frame_size = (w as usize) * (h as usize);
            let prev_luma = read_exact_vec(&mut stdin_lock, frame_size)?;
            let cur_luma = read_exact_vec(&mut stdin_lock, frame_size)?;

            let n_tiles = read_u32(&mut stdin_lock)?;
            let mut tiles = Vec::with_capacity(n_tiles as usize);
            for _ in 0..n_tiles {
                let tx = read_i32(&mut stdin_lock)?;
                let ty = read_i32(&mut stdin_lock)?;
                tiles.push((tx, ty));
            }

            if op == 1 {
                stdout_lock.write_all(&0i32.to_le_bytes())?; // status = 0 OK
                for &(tx, ty) in &tiles {
                    let res = subtile_motion(&prev_luma, &cur_luma, w, h, s, tx, ty, max_d);
                    match res {
                        Some((dx, dy, n_blocks)) => {
                            stdout_lock.write_all(&1i32.to_le_bytes())?;
                            stdout_lock.write_all(&dx.to_le_bytes())?;
                            stdout_lock.write_all(&dy.to_le_bytes())?;
                            stdout_lock.write_all(&n_blocks.to_le_bytes())?;
                        }
                        None => {
                            stdout_lock.write_all(&0i32.to_le_bytes())?;
                            stdout_lock.write_all(&0i32.to_le_bytes())?;
                            stdout_lock.write_all(&0i32.to_le_bytes())?;
                            stdout_lock.write_all(&0u32.to_le_bytes())?;
                        }
                    }
                }
                stdout_lock.flush()?;
            } else if op == 3 {
                let warmup = read_u32(&mut stdin_lock)?;
                let iters = read_u32(&mut stdin_lock)?;

                for _ in 0..warmup {
                    for &(tx, ty) in &tiles {
                        let _ = subtile_motion(&prev_luma, &cur_luma, w, h, s, tx, ty, max_d);
                    }
                }

                let mut times_ns = Vec::with_capacity(iters as usize);
                for _ in 0..iters {
                    let start = Instant::now();
                    for &(tx, ty) in &tiles {
                        let _ = subtile_motion(&prev_luma, &cur_luma, w, h, s, tx, ty, max_d);
                    }
                    let elapsed = start.elapsed().as_nanos() as u64;
                    times_ns.push(elapsed);
                }

                stdout_lock.write_all(&iters.to_le_bytes())?;
                for ns in times_ns {
                    stdout_lock.write_all(&ns.to_le_bytes())?;
                }
                stdout_lock.flush()?;
            }
        }
    }

    Ok(())
}
