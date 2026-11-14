//! In-process PyO3 extension wrapping vqa_k_st_proto8, vqa_k_st_block4, and vqa_k_gm_proto.
//! Reads luma planes zero-copy via `pyo3::buffer::PyBuffer<u8>` with a C-contiguous check
//! and releases the GIL during all kernel computations.

use pyo3::buffer::PyBuffer;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

struct ZeroCopyLuma {
    _buf: PyBuffer<u8>,
    ptr: usize,
    len: usize,
    h: usize,
    w: usize,
}

impl ZeroCopyLuma {
    fn extract(
        obj: &Bound<'_, PyAny>,
        explicit_dims: Option<(usize, usize)>,
        dims_are_hw: bool,
    ) -> PyResult<Self> {
        let buf = PyBuffer::<u8>::get_bound(obj)?;
        if !buf.is_c_contiguous() {
            return Err(PyValueError::new_err("luma buffer must be C-contiguous"));
        }
        let len = buf.item_count();
        let (w, h) = match explicit_dims {
            Some((d0, d1)) => {
                let expected = d0
                    .checked_mul(d1)
                    .ok_or_else(|| PyValueError::new_err("dimension overflow"))?;
                if d0 == 0 || d1 == 0 || len != expected {
                    return Err(PyValueError::new_err(
                        "luma buffer length does not match explicit dimensions",
                    ));
                }
                if buf.dimensions() == 2 {
                    let shape = buf.shape();
                    let bh = shape[0];
                    let bw = shape[1];
                    if !((d0 == bw && d1 == bh) || (d0 == bh && d1 == bw)) {
                        return Err(PyValueError::new_err(
                            "2D luma buffer shape does not match explicit dimensions",
                        ));
                    }
                    (bw, bh)
                } else if dims_are_hw {
                    (d1, d0)
                } else {
                    (d0, d1)
                }
            }
            None => {
                if buf.dimensions() != 2 {
                    return Err(PyValueError::new_err(
                        "luma buffer must be 2D (H, W) when dimensions are not explicitly passed",
                    ));
                }
                let shape = buf.shape();
                let bh = shape[0];
                let bw = shape[1];
                if bw == 0 || bh == 0 || len != bw * bh {
                    return Err(PyValueError::new_err("invalid 2D luma buffer shape"));
                }
                (bw, bh)
            }
        };
        let ptr = buf.buf_ptr() as *const u8 as usize;
        Ok(Self {
            _buf: buf,
            ptr,
            len,
            h,
            w,
        })
    }
}

fn parse_opt_i32(obj: &Bound<'_, PyAny>) -> PyResult<Option<i32>> {
    if obj.is_none() {
        Ok(None)
    } else {
        let v = obj.extract::<i32>()?;
        if v <= 0 {
            Ok(None)
        } else {
            Ok(Some(v))
        }
    }
}

fn parse_subtile_args(
    a2: &Bound<'_, PyAny>,
    a3: &Bound<'_, PyAny>,
    a4: &Bound<'_, PyAny>,
    a5: Option<&Bound<'_, PyAny>>,
    a6: Option<&Bound<'_, PyAny>>,
    a7: Option<&Bound<'_, PyAny>>,
) -> PyResult<(Option<(usize, usize)>, Option<i32>, i32, i32, i32)> {
    match (a5, a6, a7) {
        (None, None, None) => {
            let s_opt = parse_opt_i32(a2)?;
            let (tx, ty) = a3.extract::<(i32, i32)>()?;
            let max_d = a4.extract::<i32>()?;
            Ok((None, s_opt, tx, ty, max_d))
        }
        (Some(v5), None, None) => {
            let s_opt = parse_opt_i32(a2)?;
            let tx = a3.extract::<i32>()?;
            let ty = a4.extract::<i32>()?;
            let max_d = v5.extract::<i32>()?;
            Ok((None, s_opt, tx, ty, max_d))
        }
        (Some(v5), Some(v6), None) => {
            let w = a2.extract::<usize>()?;
            let h = a3.extract::<usize>()?;
            let s_opt = parse_opt_i32(a4)?;
            let (tx, ty) = v5.extract::<(i32, i32)>()?;
            let max_d = v6.extract::<i32>()?;
            Ok((Some((w, h)), s_opt, tx, ty, max_d))
        }
        (Some(v5), Some(v6), Some(v7)) => {
            let w = a2.extract::<usize>()?;
            let h = a3.extract::<usize>()?;
            let s_opt = parse_opt_i32(a4)?;
            let tx = v5.extract::<i32>()?;
            let ty = v6.extract::<i32>()?;
            let max_d = v7.extract::<i32>()?;
            Ok((Some((w, h)), s_opt, tx, ty, max_d))
        }
        _ => Err(PyValueError::new_err(
            "invalid arguments for subtile_motion",
        )),
    }
}

/// Wrap `vqa_k_st_proto8::subtile_motion`.
/// Supports `(prev_luma, cur_luma, s, (tx, ty), max_d)` as well as explicit `(w, h)` or unpacked `tx, ty`.
#[pyfunction]
#[pyo3(signature = (prev_luma, cur_luma, a2, a3, a4, a5=None, a6=None, a7=None))]
#[allow(clippy::too_many_arguments)]
fn st_proto8_subtile_motion(
    py: Python<'_>,
    prev_luma: &Bound<'_, PyAny>,
    cur_luma: &Bound<'_, PyAny>,
    a2: &Bound<'_, PyAny>,
    a3: &Bound<'_, PyAny>,
    a4: &Bound<'_, PyAny>,
    a5: Option<&Bound<'_, PyAny>>,
    a6: Option<&Bound<'_, PyAny>>,
    a7: Option<&Bound<'_, PyAny>>,
) -> PyResult<Option<(i32, i32, u32)>> {
    let (explicit_wh, s_opt, tx, ty, max_d) = parse_subtile_args(a2, a3, a4, a5, a6, a7)?;
    let prev = ZeroCopyLuma::extract(prev_luma, explicit_wh, false)?;
    let cur = ZeroCopyLuma::extract(cur_luma, explicit_wh, false)?;
    if prev.w != cur.w || prev.h != cur.h {
        return Err(PyValueError::new_err(
            "prev_luma and cur_luma dimensions must match",
        ));
    }
    let prev_ptr = prev.ptr;
    let prev_len = prev.len;
    let cur_ptr = cur.ptr;
    let cur_len = cur.len;
    let w = prev.w as u32;
    let h = prev.h as u32;
    let s_u32 = s_opt.map(|v| v as u32).unwrap_or(0);

    let result = py.allow_threads(move || {
        let prev_slice = unsafe { std::slice::from_raw_parts(prev_ptr as *const u8, prev_len) };
        let cur_slice = unsafe { std::slice::from_raw_parts(cur_ptr as *const u8, cur_len) };
        vqa_k_st_proto8::subtile_motion(prev_slice, cur_slice, w, h, s_u32, tx, ty, max_d)
    });
    drop(cur);
    drop(prev);
    Ok(result)
}

/// Wrap `vqa_k_st_block4::subtile_motion_opt`.
/// Supports `(prev_luma, cur_luma, s, (tx, ty), max_d)` as well as explicit `(w, h)` or unpacked `tx, ty`.
#[pyfunction]
#[pyo3(signature = (prev_luma, cur_luma, a2, a3, a4, a5=None, a6=None, a7=None))]
#[allow(clippy::too_many_arguments)]
fn st_block4_subtile_motion(
    py: Python<'_>,
    prev_luma: &Bound<'_, PyAny>,
    cur_luma: &Bound<'_, PyAny>,
    a2: &Bound<'_, PyAny>,
    a3: &Bound<'_, PyAny>,
    a4: &Bound<'_, PyAny>,
    a5: Option<&Bound<'_, PyAny>>,
    a6: Option<&Bound<'_, PyAny>>,
    a7: Option<&Bound<'_, PyAny>>,
) -> PyResult<Option<(i32, i32, i32)>> {
    let (explicit_wh, s_opt, tx, ty, max_d) = parse_subtile_args(a2, a3, a4, a5, a6, a7)?;
    let prev = ZeroCopyLuma::extract(prev_luma, explicit_wh, false)?;
    let cur = ZeroCopyLuma::extract(cur_luma, explicit_wh, false)?;
    if prev.w != cur.w || prev.h != cur.h {
        return Err(PyValueError::new_err(
            "prev_luma and cur_luma dimensions must match",
        ));
    }
    let prev_ptr = prev.ptr;
    let prev_len = prev.len;
    let cur_ptr = cur.ptr;
    let cur_len = cur.len;
    let w = prev.w as i32;
    let h = prev.h as i32;

    let result = py.allow_threads(move || {
        let prev_slice = unsafe { std::slice::from_raw_parts(prev_ptr as *const u8, prev_len) };
        let cur_slice = unsafe { std::slice::from_raw_parts(cur_ptr as *const u8, cur_len) };
        vqa_k_st_block4::subtile_motion_opt(prev_slice, cur_slice, w, h, s_opt, (tx, ty), max_d)
            .map(|r| (r.dx, r.dy, r.n_blocks))
    });
    drop(cur);
    drop(prev);
    Ok(result)
}

fn parse_tiles_and_max_d(
    x: &Bound<'_, PyAny>,
    y: &Bound<'_, PyAny>,
) -> PyResult<(Vec<(i32, i32)>, i32)> {
    if let Ok(max_d) = x.extract::<i32>() {
        let tiles = y.extract::<Vec<(i32, i32)>>()?;
        Ok((tiles, max_d))
    } else {
        let tiles = x.extract::<Vec<(i32, i32)>>()?;
        let max_d = y.extract::<i32>()?;
        Ok((tiles, max_d))
    }
}

/// Wrap `vqa_k_st_block4::subtile_motion_batch`.
/// Supports `(prev_luma, cur_luma, s, tiles, max_d)` or `(prev_luma, cur_luma, s, max_d, tiles)`
/// as well as explicit `(w, h)`.
#[pyfunction]
#[pyo3(signature = (prev_luma, cur_luma, a2, a3, a4, a5=None, a6=None))]
fn st_block4_batch(
    py: Python<'_>,
    prev_luma: &Bound<'_, PyAny>,
    cur_luma: &Bound<'_, PyAny>,
    a2: &Bound<'_, PyAny>,
    a3: &Bound<'_, PyAny>,
    a4: &Bound<'_, PyAny>,
    a5: Option<&Bound<'_, PyAny>>,
    a6: Option<&Bound<'_, PyAny>>,
) -> PyResult<Vec<Option<(i32, i32, i32)>>> {
    let (explicit_wh, s_opt, tiles, max_d) = match (a5, a6) {
        (None, None) => {
            let s_opt = parse_opt_i32(a2)?;
            let (tiles, max_d) = parse_tiles_and_max_d(a3, a4)?;
            (None, s_opt, tiles, max_d)
        }
        (Some(v5), Some(v6)) => {
            let w = a2.extract::<usize>()?;
            let h = a3.extract::<usize>()?;
            let s_opt = parse_opt_i32(a4)?;
            let (tiles, max_d) = parse_tiles_and_max_d(v5, v6)?;
            (Some((w, h)), s_opt, tiles, max_d)
        }
        _ => {
            return Err(PyValueError::new_err(
                "invalid arguments for st_block4_batch",
            ))
        }
    };

    let prev = ZeroCopyLuma::extract(prev_luma, explicit_wh, false)?;
    let cur = ZeroCopyLuma::extract(cur_luma, explicit_wh, false)?;
    if prev.w != cur.w || prev.h != cur.h {
        return Err(PyValueError::new_err(
            "prev_luma and cur_luma dimensions must match",
        ));
    }
    let prev_ptr = prev.ptr;
    let prev_len = prev.len;
    let cur_ptr = cur.ptr;
    let cur_len = cur.len;
    let w = prev.w as i32;
    let h = prev.h as i32;

    let result = py.allow_threads(move || {
        let prev_slice = unsafe { std::slice::from_raw_parts(prev_ptr as *const u8, prev_len) };
        let cur_slice = unsafe { std::slice::from_raw_parts(cur_ptr as *const u8, cur_len) };
        let s_val = match s_opt {
            Some(v) if v > 0 => v,
            _ => match vqa_k_st_block4::grid_params(w, h) {
                Ok((sg, _, _)) => sg,
                Err(_) => return vec![None; tiles.len()],
            },
        };
        vqa_k_st_block4::subtile_motion_batch(prev_slice, cur_slice, w, h, s_val, max_d, &tiles)
            .into_iter()
            .map(|opt| opt.map(|r| (r.dx, r.dy, r.n_blocks)))
            .collect()
    });
    drop(cur);
    drop(prev);
    Ok(result)
}

/// Wrap `vqa_k_gm_proto::estimate_global_motion`.
/// Supports `(prev_luma, cur_luma, max_d, k_top=16)` as well as explicit `(h, w)`.
#[pyfunction]
#[pyo3(signature = (prev_luma, cur_luma, a2, a3=None, a4=None, a5=None, *, k_top=None))]
fn gm_estimate(
    py: Python<'_>,
    prev_luma: &Bound<'_, PyAny>,
    cur_luma: &Bound<'_, PyAny>,
    a2: &Bound<'_, PyAny>,
    a3: Option<&Bound<'_, PyAny>>,
    a4: Option<&Bound<'_, PyAny>>,
    a5: Option<&Bound<'_, PyAny>>,
    k_top: Option<usize>,
) -> PyResult<Option<(i32, i32, i64)>> {
    let (explicit_hw, max_d, k_top_val) = match a4 {
        None => {
            let max_d = a2.extract::<i32>()?;
            let kt = match (a3, k_top) {
                (Some(v), _) if !v.is_none() => v.extract::<usize>()?,
                (_, Some(k)) => k,
                _ => 16usize,
            };
            (None, max_d, kt)
        }
        Some(v4) => {
            let d0 = a2.extract::<usize>()?;
            let d1 = a3
                .ok_or_else(|| PyValueError::new_err("missing dimension argument"))?
                .extract::<usize>()?;
            let max_d = v4.extract::<i32>()?;
            let kt = match (a5, k_top) {
                (Some(v), _) if !v.is_none() => v.extract::<usize>()?,
                (_, Some(k)) => k,
                _ => 16usize,
            };
            (Some((d0, d1)), max_d, kt)
        }
    };

    let prev = ZeroCopyLuma::extract(prev_luma, explicit_hw, true)?;
    let cur = ZeroCopyLuma::extract(cur_luma, explicit_hw, true)?;
    if prev.w != cur.w || prev.h != cur.h {
        return Err(PyValueError::new_err(
            "prev_luma and cur_luma dimensions must match",
        ));
    }
    let prev_ptr = prev.ptr;
    let prev_len = prev.len;
    let cur_ptr = cur.ptr;
    let cur_len = cur.len;
    let h = prev.h;
    let w = prev.w;

    let result = py.allow_threads(move || {
        let prev_slice = unsafe { std::slice::from_raw_parts(prev_ptr as *const u8, prev_len) };
        let cur_slice = unsafe { std::slice::from_raw_parts(cur_ptr as *const u8, cur_len) };
        vqa_k_gm_proto::estimate_global_motion(prev_slice, cur_slice, h, w, max_d, k_top_val)
    });
    drop(cur);
    drop(prev);
    Ok(result)
}

/// Wrap `vqa_k_gm_proto::consensus_tiles`.
/// Supports `(prev_luma, cur_luma, s, dx, dy)` as well as explicit `(h, w)`.
#[pyfunction]
#[pyo3(signature = (prev_luma, cur_luma, a2, a3, a4, a5=None, a6=None))]
fn gm_consensus(
    py: Python<'_>,
    prev_luma: &Bound<'_, PyAny>,
    cur_luma: &Bound<'_, PyAny>,
    a2: &Bound<'_, PyAny>,
    a3: &Bound<'_, PyAny>,
    a4: &Bound<'_, PyAny>,
    a5: Option<&Bound<'_, PyAny>>,
    a6: Option<&Bound<'_, PyAny>>,
) -> PyResult<Vec<bool>> {
    let (explicit_hw, s_opt, dx, dy) = match (a5, a6) {
        (None, None) => {
            let s_opt = parse_opt_i32(a2)?;
            let dx = a3.extract::<i32>()?;
            let dy = a4.extract::<i32>()?;
            (None, s_opt, dx, dy)
        }
        (Some(v5), Some(v6)) => {
            let d0 = a2.extract::<usize>()?;
            let d1 = a3.extract::<usize>()?;
            let s_opt = parse_opt_i32(a4)?;
            let dx = v5.extract::<i32>()?;
            let dy = v6.extract::<i32>()?;
            (Some((d0, d1)), s_opt, dx, dy)
        }
        _ => return Err(PyValueError::new_err("invalid arguments for gm_consensus")),
    };

    let prev = ZeroCopyLuma::extract(prev_luma, explicit_hw, true)?;
    let cur = ZeroCopyLuma::extract(cur_luma, explicit_hw, true)?;
    if prev.w != cur.w || prev.h != cur.h {
        return Err(PyValueError::new_err(
            "prev_luma and cur_luma dimensions must match",
        ));
    }
    let prev_ptr = prev.ptr;
    let prev_len = prev.len;
    let cur_ptr = cur.ptr;
    let cur_len = cur.len;
    let h = prev.h;
    let w = prev.w;

    let result = py.allow_threads(move || {
        let prev_slice = unsafe { std::slice::from_raw_parts(prev_ptr as *const u8, prev_len) };
        let cur_slice = unsafe { std::slice::from_raw_parts(cur_ptr as *const u8, cur_len) };
        let s_val = match s_opt {
            Some(v) if v > 0 => v as usize,
            _ => vqa_k_gm_proto::grid_params(w, h).0,
        };
        vqa_k_gm_proto::consensus_tiles(prev_slice, cur_slice, h, w, s_val, dx, dy)
    });
    drop(cur);
    drop(prev);
    Ok(result)
}

#[pymodule]
fn vqa_native_v2k(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(st_proto8_subtile_motion, m)?)?;
    m.add_function(wrap_pyfunction!(st_block4_subtile_motion, m)?)?;
    m.add_function(wrap_pyfunction!(st_block4_batch, m)?)?;
    m.add_function(wrap_pyfunction!(gm_estimate, m)?)?;
    m.add_function(wrap_pyfunction!(gm_consensus, m)?)?;
    Ok(())
}
