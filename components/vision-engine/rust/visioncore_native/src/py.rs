//! PyO3 surface (RT1 + RT2 kernels): abi_version, layout_check, panic_probe, and S0-S4 hot kernels.
//! Every #[pyfunction] body runs inside guard() = catch_unwind (SW-0092);
//! tests/test_rt1_native_build.py statically checks that each pyfunction body is exactly one guard(...) call.
#![allow(
    clippy::too_many_arguments,
    clippy::type_complexity,
    clippy::useless_conversion
)]
use pyo3::buffer::{Element, PyBuffer};
use pyo3::exceptions::{PyIndexError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use std::panic::{catch_unwind, AssertUnwindSafe};

use crate::isqrt::{self as isqrt_mod, active_mutant_name, set_mutant_by_name};
use crate::par::{self, MatchRegionInput};
use crate::s0;
use crate::s1;
use crate::{ABI_VERSION, TILE_FIELDS, TILE_SIZE};

/// A Rust panic becomes a Python RuntimeError instead of unwinding across the FFI boundary.
fn guard<T>(name: &str, f: impl FnOnce() -> PyResult<T>) -> PyResult<T> {
    match catch_unwind(AssertUnwindSafe(f)) {
        Ok(r) => r,
        Err(_) => Err(PyRuntimeError::new_err(format!(
            "rust panic caught: {}",
            name
        ))),
    }
}

struct ZeroCopyBuf<T: Element> {
    _buf: PyBuffer<T>,
    ptr: usize,
    len: usize,
    shape: Vec<usize>,
}

impl<T: Element> ZeroCopyBuf<T> {
    fn extract(obj: &Bound<'_, PyAny>, label: &str) -> PyResult<Self> {
        let buf = PyBuffer::<T>::get_bound(obj)?;
        if !buf.is_c_contiguous() {
            return Err(PyValueError::new_err(format!(
                "{} buffer must be C-contiguous",
                label
            )));
        }
        let len = buf.item_count();
        let shape = buf.shape().to_vec();
        let ptr = buf.buf_ptr() as *const T as usize;
        Ok(Self {
            _buf: buf,
            ptr,
            len,
            shape,
        })
    }

    #[inline]
    unsafe fn as_slice(&self) -> &[T] {
        if self.len == 0 {
            &[]
        } else {
            std::slice::from_raw_parts(self.ptr as *const T, self.len)
        }
    }
}

struct ZeroCopyMutBuf<T: Element> {
    _buf: PyBuffer<T>,
    ptr: usize,
    len: usize,
}

impl<T: Element> ZeroCopyMutBuf<T> {
    fn extract(obj: &Bound<'_, PyAny>, label: &str) -> PyResult<Self> {
        let buf = PyBuffer::<T>::get_bound(obj)?;
        if buf.readonly() {
            return Err(PyValueError::new_err(format!(
                "{} buffer must be writable",
                label
            )));
        }
        if !buf.is_c_contiguous() {
            return Err(PyValueError::new_err(format!(
                "{} buffer must be C-contiguous",
                label
            )));
        }
        let len = buf.item_count();
        let ptr = buf.buf_ptr() as *mut T as usize;
        Ok(Self {
            _buf: buf,
            ptr,
            len,
        })
    }

    #[inline]
    unsafe fn as_mut_slice(&mut self) -> &mut [T] {
        if self.len == 0 {
            &mut []
        } else {
            std::slice::from_raw_parts_mut(self.ptr as *mut T, self.len)
        }
    }
}

fn np_empty<'py>(
    py: Python<'py>,
    shape: impl IntoPy<Py<PyTuple>>,
    dtype: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let np = PyModule::import_bound(py, "numpy")?;
    np.call_method1("empty", (shape.into_py(py), dtype))
}

fn extract_2d_shape(shape: &[usize], label: &str) -> PyResult<(usize, usize)> {
    if shape.len() != 2 {
        return Err(PyValueError::new_err(format!(
            "{} must be a 2D array",
            label
        )));
    }
    Ok((shape[0], shape[1]))
}

fn extract_u8_or_bool<'py>(
    obj: &Bound<'py, PyAny>,
    label: &str,
) -> PyResult<(Option<Bound<'py, PyAny>>, ZeroCopyBuf<u8>)> {
    if let Ok(b) = ZeroCopyBuf::<u8>::extract(obj, label) {
        return Ok((None, b));
    }
    if let Ok(dt) = obj.getattr("dtype") {
        let kind: String = dt.getattr("kind")?.extract()?;
        if kind == "b" {
            let is_c: bool = obj.getattr("flags")?.getattr("c_contiguous")?.extract()?;
            if !is_c {
                return Err(PyValueError::new_err(format!(
                    "{} buffer must be C-contiguous",
                    label
                )));
            }
            let view = obj.call_method1("view", ("u1",))?;
            let b = ZeroCopyBuf::<u8>::extract(&view, label)?;
            return Ok((Some(view), b));
        }
    }
    let b = ZeroCopyBuf::<u8>::extract(obj, label)?;
    Ok((None, b))
}

fn extract_tx_ty_i64(
    py: Python<'_>,
    tx: &Bound<'_, PyAny>,
    ty: &Bound<'_, PyAny>,
    w_tiles: i64,
    h_tiles: i64,
) -> PyResult<(Vec<i64>, Vec<i64>, Vec<usize>, bool)> {
    // Fast zero-copy path for 1D C-contiguous int64 or int32 arrays of matching length
    if let (Ok(bx), Ok(by)) = (
        PyBuffer::<i64>::get_bound(tx),
        PyBuffer::<i64>::get_bound(ty),
    ) {
        if bx.is_c_contiguous()
            && by.is_c_contiguous()
            && bx.dimensions() == 1
            && by.dimensions() == 1
            && bx.item_count() == by.item_count()
        {
            let n = bx.item_count();
            let sx = if n == 0 {
                &[]
            } else {
                unsafe { std::slice::from_raw_parts(bx.buf_ptr() as *const i64, n) }
            };
            let sy = if n == 0 {
                &[]
            } else {
                unsafe { std::slice::from_raw_parts(by.buf_ptr() as *const i64, n) }
            };
            return Ok((sx.to_vec(), sy.to_vec(), vec![n], false));
        }
    }
    if let (Ok(bx), Ok(by)) = (
        PyBuffer::<i32>::get_bound(tx),
        PyBuffer::<i32>::get_bound(ty),
    ) {
        if bx.is_c_contiguous()
            && by.is_c_contiguous()
            && bx.dimensions() == 1
            && by.dimensions() == 1
            && bx.item_count() == by.item_count()
        {
            let n = bx.item_count();
            let sx = if n == 0 {
                &[]
            } else {
                unsafe { std::slice::from_raw_parts(bx.buf_ptr() as *const i32, n) }
            };
            let sy = if n == 0 {
                &[]
            } else {
                unsafe { std::slice::from_raw_parts(by.buf_ptr() as *const i32, n) }
            };
            let vx: Vec<i64> = sx.iter().map(|&v| v as i64).collect();
            let vy: Vec<i64> = sy.iter().map(|&v| v as i64).collect();
            return Ok((vx, vy, vec![n], false));
        }
    }

    let np = PyModule::import_bound(py, "numpy")?;
    let tx_a = np.call_method1("asarray", (tx,))?;
    let ty_a = np.call_method1("asarray", (ty,))?;
    let bcast = np.call_method1("broadcast_arrays", (tx_a, ty_a))?;
    let btx = bcast.get_item(0)?;
    let bty = bcast.get_item(1)?;

    let int_type = np.getattr("integer")?;
    let tx_is_int: bool = np
        .call_method1("issubdtype", (btx.getattr("dtype")?, &int_type))?
        .extract()?;
    let ty_is_int: bool = np
        .call_method1("issubdtype", (bty.getattr("dtype")?, &int_type))?
        .extract()?;
    if !tx_is_int || !ty_is_int {
        return Err(PyIndexError::new_err("tile index out of bounds"));
    }

    // Check unsigned bounds before casting to signed int64
    let tx_kind: String = btx.getattr("dtype")?.getattr("kind")?.extract()?;
    let ty_kind: String = bty.getattr("dtype")?.getattr("kind")?.extract()?;
    if tx_kind == "u" {
        let max_tx: u64 = if btx.getattr("size")?.extract::<usize>()? > 0 {
            btx.call_method0("max")?.extract()?
        } else {
            0
        };
        if max_tx >= (w_tiles.max(0) as u64) {
            return Err(PyIndexError::new_err("tile index out of bounds"));
        }
    }
    if ty_kind == "u" {
        let max_ty: u64 = if bty.getattr("size")?.extract::<usize>()? > 0 {
            bty.call_method0("max")?.extract()?
        } else {
            0
        };
        if max_ty >= (h_tiles.max(0) as u64) {
            return Err(PyIndexError::new_err("tile index out of bounds"));
        }
    }

    let shape: Vec<usize> = btx.getattr("shape")?.extract()?;
    let is_scalar = shape.is_empty();
    let tx_c = np.call_method1("ascontiguousarray", (btx.call_method0("ravel")?, "int64"))?;
    let ty_c = np.call_method1("ascontiguousarray", (bty.call_method0("ravel")?, "int64"))?;
    let bx = ZeroCopyBuf::<i64>::extract(&tx_c, "tx")?;
    let by = ZeroCopyBuf::<i64>::extract(&ty_c, "ty")?;
    let vx = unsafe { bx.as_slice().to_vec() };
    let vy = unsafe { by.as_slice().to_vec() };
    Ok((vx, vy, shape, is_scalar))
}

fn format_sad_cnt_output(
    py: Python<'_>,
    sad_arr: Bound<'_, PyAny>,
    cnt_arr: Bound<'_, PyAny>,
    shape: &[usize],
    is_scalar: bool,
) -> PyResult<PyObject> {
    if is_scalar {
        let sad_s = sad_arr
            .call_method1("reshape", (PyTuple::empty_bound(py),))?
            .get_item(PyTuple::empty_bound(py))?;
        let cnt_s = cnt_arr
            .call_method1("reshape", (PyTuple::empty_bound(py),))?
            .get_item(PyTuple::empty_bound(py))?;
        return Ok((sad_s, cnt_s).into_py(py));
    }
    if shape.len() == 1 {
        return Ok((sad_arr, cnt_arr).into_py(py));
    }
    let st = PyTuple::new_bound(py, shape);
    let sad_r = sad_arr.call_method1("reshape", (&st,))?;
    let cnt_r = cnt_arr.call_method1("reshape", (&st,))?;
    Ok((sad_r, cnt_r).into_py(py))
}

fn impl_tile_sad_windows(
    py: Python<'_>,
    y_cur: &Bound<'_, PyAny>,
    y_prev: &Bound<'_, PyAny>,
    c: (i64, i64),
    s: usize,
    tx: &Bound<'_, PyAny>,
    ty: &Bound<'_, PyAny>,
) -> PyResult<PyObject> {
    if s == 0 {
        return Err(PyValueError::new_err("s must be positive"));
    }
    if PyBuffer::<i16>::get_bound(y_cur).is_ok() || PyBuffer::<i16>::get_bound(y_prev).is_ok() {
        let yc_i16 = ZeroCopyBuf::<i16>::extract(y_cur, "y_cur")?;
        let yp_i16 = ZeroCopyBuf::<i16>::extract(y_prev, "y_prev")?;
        let (h, w) = extract_2d_shape(&yc_i16.shape, "y_cur")?;
        let (hp, wp) = extract_2d_shape(&yp_i16.shape, "y_prev")?;
        if (h, w) != (hp, wp) {
            return Err(PyValueError::new_err("y_cur and y_prev shapes must match"));
        }
        let w_tiles = (w / s) as i64;
        let h_tiles = (h / s) as i64;

        // Ultra-fast path when tx and ty are 1D C-contiguous int64 arrays
        if let (Ok(bx), Ok(by)) = (
            ZeroCopyBuf::<i64>::extract(tx, "tx"),
            ZeroCopyBuf::<i64>::extract(ty, "ty"),
        ) {
            if bx.shape.len() == 1 && by.shape.len() == 1 && bx.len == by.len {
                let n = bx.len;
                let sad_arr = np_empty(py, (n,), "int64")?;
                let cnt_arr = np_empty(py, (n,), "int64")?;
                let mut sad_buf = ZeroCopyMutBuf::<i64>::extract(&sad_arr, "sad")?;
                let mut cnt_buf = ZeroCopyMutBuf::<i64>::extract(&cnt_arr, "count")?;
                let res = py.allow_threads(|| unsafe {
                    s0::tile_sad_windows_into(
                        yc_i16.as_slice(),
                        yp_i16.as_slice(),
                        h,
                        w,
                        c.0,
                        c.1,
                        s,
                        bx.as_slice(),
                        by.as_slice(),
                        sad_buf.as_mut_slice(),
                        cnt_buf.as_mut_slice(),
                    )
                });
                drop(sad_buf);
                drop(cnt_buf);
                if res.is_err() {
                    return Err(PyIndexError::new_err("tile index out of bounds"));
                }
                return Ok((sad_arr, cnt_arr).into_py(py));
            }
        }

        let (vx, vy, shape, is_scalar) = extract_tx_ty_i64(py, tx, ty, w_tiles, h_tiles)?;
        let n = vx.len();
        let sad_arr = np_empty(py, (n,), "int64")?;
        let cnt_arr = np_empty(py, (n,), "int64")?;
        let mut sad_buf = ZeroCopyMutBuf::<i64>::extract(&sad_arr, "sad")?;
        let mut cnt_buf = ZeroCopyMutBuf::<i64>::extract(&cnt_arr, "count")?;
        let res = py.allow_threads(|| unsafe {
            s0::tile_sad_windows_into(
                yc_i16.as_slice(),
                yp_i16.as_slice(),
                h,
                w,
                c.0,
                c.1,
                s,
                &vx,
                &vy,
                sad_buf.as_mut_slice(),
                cnt_buf.as_mut_slice(),
            )
        });
        drop(sad_buf);
        drop(cnt_buf);
        if res.is_err() {
            return Err(PyIndexError::new_err("tile index out of bounds"));
        }
        return format_sad_cnt_output(py, sad_arr, cnt_arr, &shape, is_scalar);
    }

    let yc_i64 = ZeroCopyBuf::<i64>::extract(y_cur, "y_cur")?;
    let yp_i64 = ZeroCopyBuf::<i64>::extract(y_prev, "y_prev")?;
    let (h, w) = extract_2d_shape(&yc_i64.shape, "y_cur")?;
    let (hp, wp) = extract_2d_shape(&yp_i64.shape, "y_prev")?;
    if (h, w) != (hp, wp) {
        return Err(PyValueError::new_err("y_cur and y_prev shapes must match"));
    }
    let w_tiles = (w / s) as i64;
    let h_tiles = (h / s) as i64;
    let (vx, vy, shape, is_scalar) = extract_tx_ty_i64(py, tx, ty, w_tiles, h_tiles)?;
    let n = vx.len();
    let sad_arr = np_empty(py, (n,), "int64")?;
    let cnt_arr = np_empty(py, (n,), "int64")?;
    let mut sad_buf = ZeroCopyMutBuf::<i64>::extract(&sad_arr, "sad")?;
    let mut cnt_buf = ZeroCopyMutBuf::<i64>::extract(&cnt_arr, "count")?;
    let res = py.allow_threads(|| unsafe {
        s0::tile_sad_windows_i64_into(
            yc_i64.as_slice(),
            yp_i64.as_slice(),
            h,
            w,
            c.0,
            c.1,
            s,
            &vx,
            &vy,
            sad_buf.as_mut_slice(),
            cnt_buf.as_mut_slice(),
        )
    });
    drop(sad_buf);
    drop(cnt_buf);
    if res.is_err() {
        return Err(PyIndexError::new_err("tile index out of bounds"));
    }
    format_sad_cnt_output(py, sad_arr, cnt_arr, &shape, is_scalar)
}

#[allow(clippy::too_many_arguments)]
fn impl_tile_sad(
    py: Python<'_>,
    y_cur: &Bound<'_, PyAny>,
    y_prev: &Bound<'_, PyAny>,
    c: (i64, i64),
    s: usize,
    _gh: usize,
    _gw: usize,
    tx: &Bound<'_, PyAny>,
    ty: &Bound<'_, PyAny>,
) -> PyResult<PyObject> {
    let pair = impl_tile_sad_windows(py, y_cur, y_prev, c, s, tx, ty)?;
    let bound = pair.bind(py);
    let sad = bound.get_item(0)?;
    let cnt = bound.get_item(1)?;
    let full = (s * s) as i64;
    let valid = cnt.call_method1("__eq__", (full,))?;
    Ok((sad, valid).into_py(py))
}

#[allow(clippy::too_many_arguments)]
fn impl_tile_sad_clip(
    py: Python<'_>,
    y_cur: &Bound<'_, PyAny>,
    y_prev: &Bound<'_, PyAny>,
    c: (i64, i64),
    s: usize,
    _gh: usize,
    _gw: usize,
    tx: &Bound<'_, PyAny>,
    ty: &Bound<'_, PyAny>,
) -> PyResult<PyObject> {
    impl_tile_sad_windows(py, y_cur, y_prev, c, s, tx, ty)
}

fn impl_axis_shift(
    py: Python<'_>,
    cur: &Bound<'_, PyAny>,
    prev: &Bound<'_, PyAny>,
    dmax: i64,
) -> PyResult<i64> {
    if let (Ok(bc), Ok(bp)) = (
        ZeroCopyBuf::<i64>::extract(cur, "cur"),
        ZeroCopyBuf::<i64>::extract(prev, "prev"),
    ) {
        if bc.shape.len() != 1 || bp.shape.len() != 1 || bc.len != bp.len {
            return Err(PyValueError::new_err(
                "cur and prev must be 1D arrays of equal length",
            ));
        }
        let res =
            py.allow_threads(|| unsafe { par::axis_shift(bc.as_slice(), bp.as_slice(), dmax) });
        return Ok(res);
    }
    // Check if non-contiguous buffer was passed
    if let Ok(b) = PyBuffer::<i64>::get_bound(cur) {
        if !b.is_c_contiguous() {
            return Err(PyValueError::new_err("cur buffer must be C-contiguous"));
        }
    }
    if let Ok(b) = PyBuffer::<i64>::get_bound(prev) {
        if !b.is_c_contiguous() {
            return Err(PyValueError::new_err("prev buffer must be C-contiguous"));
        }
    }
    let vc: Vec<i64> = cur.extract()?;
    let vp: Vec<i64> = prev.extract()?;
    if vc.len() != vp.len() {
        return Err(PyValueError::new_err("cur and prev must have equal length"));
    }
    Ok(py.allow_threads(|| par::axis_shift(&vc, &vp, dmax)))
}

fn impl_global_vector(
    py: Python<'_>,
    y_cur: &Bound<'_, PyAny>,
    y_prev: &Bound<'_, PyAny>,
    s: i64,
) -> PyResult<(i64, i64)> {
    if PyBuffer::<i16>::get_bound(y_cur).is_ok() || PyBuffer::<i16>::get_bound(y_prev).is_ok() {
        let yc = ZeroCopyBuf::<i16>::extract(y_cur, "y_cur")?;
        let yp = ZeroCopyBuf::<i16>::extract(y_prev, "y_prev")?;
        let (h, w) = extract_2d_shape(&yc.shape, "y_cur")?;
        let (hp, wp) = extract_2d_shape(&yp.shape, "y_prev")?;
        if (h, w) != (hp, wp) {
            return Err(PyValueError::new_err("y_cur and y_prev shapes must match"));
        }
        let mv = py
            .allow_threads(|| unsafe { par::global_vector(yc.as_slice(), yp.as_slice(), h, w, s) });
        return Ok(mv);
    }
    let yc = ZeroCopyBuf::<i64>::extract(y_cur, "y_cur")?;
    let yp = ZeroCopyBuf::<i64>::extract(y_prev, "y_prev")?;
    let (h, w) = extract_2d_shape(&yc.shape, "y_cur")?;
    let (hp, wp) = extract_2d_shape(&yp.shape, "y_prev")?;
    if (h, w) != (hp, wp) {
        return Err(PyValueError::new_err("y_cur and y_prev shapes must match"));
    }
    let mv = py
        .allow_threads(|| unsafe { par::global_vector_i64(yc.as_slice(), yp.as_slice(), h, w, s) });
    Ok(mv)
}

fn impl_tile_sums(
    py: Python<'_>,
    absdiff: &Bound<'_, PyAny>,
    s: usize,
    gh: usize,
    gw: usize,
) -> PyResult<PyObject> {
    let out = np_empty(py, (gh, gw), "int64")?;
    let mut out_buf = ZeroCopyMutBuf::<i64>::extract(&out, "out")?;
    if let Ok(b16) = ZeroCopyBuf::<i16>::extract(absdiff, "absdiff") {
        if b16.len != gh * s * gw * s {
            return Err(PyValueError::new_err("absdiff size mismatch"));
        }
        py.allow_threads(|| unsafe {
            par::tile_sums_i16_into(b16.as_slice(), s, gh, gw, out_buf.as_mut_slice());
        });
        drop(out_buf);
        return Ok(out.unbind());
    }
    if let Ok(b32) = ZeroCopyBuf::<i32>::extract(absdiff, "absdiff") {
        if b32.len != gh * s * gw * s {
            return Err(PyValueError::new_err("absdiff size mismatch"));
        }
        py.allow_threads(|| unsafe {
            par::tile_sums_i32_into(b32.as_slice(), s, gh, gw, out_buf.as_mut_slice());
        });
        drop(out_buf);
        return Ok(out.unbind());
    }
    let b64 = ZeroCopyBuf::<i64>::extract(absdiff, "absdiff")?;
    if b64.len != gh * s * gw * s {
        return Err(PyValueError::new_err("absdiff size mismatch"));
    }
    py.allow_threads(|| unsafe {
        par::tile_sums_i64_into(b64.as_slice(), s, gh, gw, out_buf.as_mut_slice());
    });
    drop(out_buf);
    Ok(out.unbind())
}

fn impl_s4_islands(py: Python<'_>, cells_dict: &Bound<'_, PyDict>) -> PyResult<PyObject> {
    let mut cells: Vec<(i32, i32, u16)> = Vec::with_capacity(cells_dict.len());
    for (k, v) in cells_dict.iter() {
        let (tx, ty): (i32, i32) = k.extract()?;
        let m: u16 = if let Ok(code) = v.extract::<u16>() {
            code
        } else {
            // Support tuple values where morton is element 1 or 0
            let tup = v.downcast::<PyTuple>()?;
            if tup.len() >= 2 {
                tup.get_item(1)?.extract::<u16>()?
            } else {
                tup.get_item(0)?.extract::<u16>()?
            }
        };
        cells.push((tx, ty, m));
    }
    let comps = py.allow_threads(|| par::s4_islands(&cells));
    let out_list = PyList::empty_bound(py);
    for comp in comps {
        let sub = PyList::empty_bound(py);
        for (tx, ty) in comp {
            sub.append((tx, ty))?;
        }
        out_list.append(sub)?;
    }
    Ok(out_list.into())
}

fn impl_islands(py: Python<'_>, arg: &Bound<'_, PyAny>) -> PyResult<PyObject> {
    if let Ok(dict) = arg.downcast::<PyDict>() {
        return impl_s4_islands(py, dict);
    }
    let (_keep, mask_buf) = extract_u8_or_bool(arg, "mask")?;
    let (gh, gw) = extract_2d_shape(&mask_buf.shape, "mask")?;
    let lab = np_empty(py, (gh, gw), "int32")?;
    let mut lab_buf = ZeroCopyMutBuf::<i32>::extract(&lab, "lab")?;
    let n = py.allow_threads(|| unsafe {
        par::s2_islands_into(mask_buf.as_slice(), gh, gw, lab_buf.as_mut_slice())
    });
    drop(lab_buf);
    Ok((lab, n).into_py(py))
}

fn impl_island_vector(
    py: Python<'_>,
    lab_mask: &Bound<'_, PyAny>,
    y_cur: &Bound<'_, PyAny>,
    y_prev: &Bound<'_, PyAny>,
    s: usize,
) -> PyResult<(i64, i64)> {
    let (_keep, mb) = extract_u8_or_bool(lab_mask, "lab_mask")?;
    let (gh, gw) = extract_2d_shape(&mb.shape, "lab_mask")?;
    if let (Ok(yc), Ok(yp)) = (
        ZeroCopyBuf::<i16>::extract(y_cur, "y_cur"),
        ZeroCopyBuf::<i16>::extract(y_prev, "y_prev"),
    ) {
        let (h, w) = extract_2d_shape(&yc.shape, "y_cur")?;
        let (hp, wp) = extract_2d_shape(&yp.shape, "y_prev")?;
        if (h, w) != (hp, wp) {
            return Err(PyValueError::new_err("y_cur and y_prev shapes must match"));
        }
        let res = py.allow_threads(|| unsafe {
            par::island_vector_i16(mb.as_slice(), gh, gw, yc.as_slice(), yp.as_slice(), h, w, s)
        });
        return res.map_err(PyValueError::new_err);
    }
    let yc = ZeroCopyBuf::<i64>::extract(y_cur, "y_cur")?;
    let yp = ZeroCopyBuf::<i64>::extract(y_prev, "y_prev")?;
    let (h, w) = extract_2d_shape(&yc.shape, "y_cur")?;
    let (hp, wp) = extract_2d_shape(&yp.shape, "y_prev")?;
    if (h, w) != (hp, wp) {
        return Err(PyValueError::new_err("y_cur and y_prev shapes must match"));
    }
    let res = py.allow_threads(|| unsafe {
        par::island_vector_i64(mb.as_slice(), gh, gw, yc.as_slice(), yp.as_slice(), h, w, s)
    });
    res.map_err(PyValueError::new_err)
}

fn impl_luma_of_padded(py: Python<'_>, padded: &Bound<'_, PyAny>) -> PyResult<PyObject> {
    let rgb = ZeroCopyBuf::<u8>::extract(padded, "padded")?;
    if rgb.shape.len() != 3 || rgb.shape[2] != 3 {
        return Err(PyValueError::new_err("padded must have shape (H, W, 3)"));
    }
    let h = rgb.shape[0];
    let w = rgb.shape[1];
    let out = np_empty(py, (h, w), "int16")?;
    let mut out_buf = ZeroCopyMutBuf::<i16>::extract(&out, "luma")?;
    py.allow_threads(|| unsafe {
        s0::luma_of_padded_into(rgb.as_slice(), out_buf.as_mut_slice());
    });
    drop(out_buf);
    Ok(out.unbind())
}

fn impl_ycocg(py: Python<'_>, rgb_obj: &Bound<'_, PyAny>) -> PyResult<PyObject> {
    let rgb = ZeroCopyBuf::<u8>::extract(rgb_obj, "rgb")?;
    if rgb.shape.is_empty() || *rgb.shape.last().unwrap() != 3 {
        return Err(PyValueError::new_err("rgb must have last dimension 3"));
    }
    let plane_shape = &rgb.shape[..rgb.shape.len() - 1];
    let st = PyTuple::new_bound(py, plane_shape);
    let y_arr = np_empty(py, &st, "int16")?;
    let co_arr = np_empty(py, &st, "int16")?;
    let cg_arr = np_empty(py, &st, "int16")?;
    let mut y_buf = ZeroCopyMutBuf::<i16>::extract(&y_arr, "y")?;
    let mut co_buf = ZeroCopyMutBuf::<i16>::extract(&co_arr, "co")?;
    let mut cg_buf = ZeroCopyMutBuf::<i16>::extract(&cg_arr, "cg")?;
    py.allow_threads(|| unsafe {
        s0::ycocg_into(
            rgb.as_slice(),
            y_buf.as_mut_slice(),
            co_buf.as_mut_slice(),
            cg_buf.as_mut_slice(),
        );
    });
    drop(y_buf);
    drop(co_buf);
    drop(cg_buf);
    Ok((y_arr, co_arr, cg_arr).into_py(py))
}

fn impl_isqrt(py: Python<'_>, v: &Bound<'_, PyAny>) -> PyResult<PyObject> {
    if let Ok(vb) = ZeroCopyBuf::<i64>::extract(v, "v") {
        let is_scalar = vb.shape.is_empty();
        let n = vb.len;
        let out = np_empty(py, (n,), "int64")?;
        let mut out_buf = ZeroCopyMutBuf::<i64>::extract(&out, "out")?;
        py.allow_threads(|| unsafe {
            isqrt_mod::isqrt_slice(vb.as_slice(), out_buf.as_mut_slice());
        });
        drop(out_buf);
        if is_scalar {
            let s = out
                .call_method1("reshape", (PyTuple::empty_bound(py),))?
                .get_item(PyTuple::empty_bound(py))?;
            return Ok(s.unbind());
        }
        if vb.shape.len() == 1 {
            return Ok(out.unbind());
        }
        let st = PyTuple::new_bound(py, &vb.shape);
        return Ok(out.call_method1("reshape", (st,))?.unbind());
    }
    if let Ok(b) = PyBuffer::<i64>::get_bound(v) {
        if !b.is_c_contiguous() {
            return Err(PyValueError::new_err("v buffer must be C-contiguous"));
        }
    }
    let np = PyModule::import_bound(py, "numpy")?;
    let v_arr = np.call_method1("asarray", (v, "int64"))?;
    let shape: Vec<usize> = v_arr.getattr("shape")?.extract()?;
    let is_scalar = shape.is_empty();
    let v_c = np.call_method1("ascontiguousarray", (v_arr.call_method0("ravel")?, "int64"))?;
    let vb = ZeroCopyBuf::<i64>::extract(&v_c, "v")?;
    let n = vb.len;
    let out = np_empty(py, (n,), "int64")?;
    let mut out_buf = ZeroCopyMutBuf::<i64>::extract(&out, "out")?;
    py.allow_threads(|| unsafe {
        isqrt_mod::isqrt_slice(vb.as_slice(), out_buf.as_mut_slice());
    });
    drop(out_buf);
    if is_scalar {
        let s = out
            .call_method1("reshape", (PyTuple::empty_bound(py),))?
            .get_item(PyTuple::empty_bound(py))?;
        return Ok(s.unbind());
    }
    if shape.len() == 1 {
        return Ok(out.unbind());
    }
    let st = PyTuple::new_bound(py, &shape);
    Ok(out.call_method1("reshape", (st,))?.unbind())
}

#[allow(clippy::too_many_arguments)]
fn impl_compute(
    py: Python<'_>,
    out: &Bound<'_, PyAny>,
    idx: &Bound<'_, PyAny>,
    padded: &Bound<'_, PyAny>,
    grid: &Bound<'_, PyAny>,
    p: &Bound<'_, PyAny>,
    y_plane: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let out_u8 = out.call_method1("view", ("u1",))?;
    let mut out_buf = ZeroCopyMutBuf::<u8>::extract(&out_u8, "out")?;
    let rgb_buf = ZeroCopyBuf::<u8>::extract(padded, "padded")?;
    let y_buf = ZeroCopyBuf::<i16>::extract(y_plane, "y_plane")?;

    let s: usize = grid.getattr("s")?.extract()?;
    let gw: usize = grid.getattr("gw")?.extract()?;
    let gh: usize = grid.getattr("gh")?.extract()?;
    let tx_obj = grid.getattr("tx")?;
    let ty_obj = grid.getattr("ty")?;
    let pf_obj = grid.call_method0("pad_flags")?;

    let tx_buf = ZeroCopyBuf::<i32>::extract(&tx_obj, "grid.tx")?;
    let ty_buf = ZeroCopyBuf::<i32>::extract(&ty_obj, "grid.ty")?;
    let (_keep_pf, pf_buf) = extract_u8_or_bool(&pf_obj, "grid.pad_flags")?;

    let tau_edge: i16 = p.get_item("TAU_EDGE")?.extract()?;

    if let Ok(idx_buf) = ZeroCopyBuf::<i64>::extract(idx, "idx") {
        py.allow_threads(|| unsafe {
            s1::compute_tiles_into(
                out_buf.as_mut_slice(),
                idx_buf.as_slice(),
                rgb_buf.as_slice(),
                y_buf.as_slice(),
                s,
                gw,
                gh,
                tx_buf.as_slice(),
                ty_buf.as_slice(),
                pf_buf.as_slice(),
                tau_edge,
            );
        });
        return Ok(());
    }

    let idx_vec: Vec<i64> = idx.extract()?;
    py.allow_threads(|| unsafe {
        s1::compute_tiles_into(
            out_buf.as_mut_slice(),
            &idx_vec,
            rgb_buf.as_slice(),
            y_buf.as_slice(),
            s,
            gw,
            gh,
            tx_buf.as_slice(),
            ty_buf.as_slice(),
            pf_buf.as_slice(),
            tau_edge,
        );
    });
    Ok(())
}

fn impl_describe_padded_tiles(
    py: Python<'_>,
    padded: &Bound<'_, PyAny>,
    grid: &Bound<'_, PyAny>,
) -> PyResult<PyObject> {
    let desc_mod = PyModule::import_bound(py, "visioncore.desc")?;
    let params_mod = PyModule::import_bound(py, "visioncore.params")?;
    let p = params_mod.getattr("PARAMS")?;
    let tile_dtype = desc_mod.getattr("TILE_DTYPE")?;
    let n_tiles: usize = grid.getattr("N")?.extract()?;
    let np = PyModule::import_bound(py, "numpy")?;
    let out = np.call_method1("zeros", (n_tiles, tile_dtype))?;
    out.set_item("morton", grid.getattr("mortons")?)?;
    let y_plane = impl_luma_of_padded(py, padded)?;
    let idx: Vec<i64> = (0..n_tiles as i64).collect();
    let idx_py = np.call_method1("asarray", (idx, "int64"))?;
    impl_compute(py, &out, &idx_py, padded, grid, &p, y_plane.bind(py))?;
    let xh = desc_mod.call_method1("_hash_tiles", (padded, grid, &idx_py))?;
    out.set_item("xh64", xh)?;
    Ok(out.unbind())
}

fn impl_pad(
    py: Python<'_>,
    grid_or_frame: &Bound<'_, PyAny>,
    frame_opt: Option<&Bound<'_, PyAny>>,
) -> PyResult<PyObject> {
    let (grid, frame) = match frame_opt {
        Some(f) => (grid_or_frame, f),
        None => {
            return Err(PyValueError::new_err(
                "pad requires (grid, frame) arguments",
            ))
        }
    };
    let w: usize = grid.getattr("W")?.extract()?;
    let h: usize = grid.getattr("H")?.extract()?;
    let s: usize = grid.getattr("s")?.extract()?;
    let gw: usize = grid.getattr("gw")?.extract()?;
    let gh: usize = grid.getattr("gh")?.extract()?;
    let pw: usize = grid.getattr("pw")?.extract()?;
    let ph: usize = grid.getattr("ph")?.extract()?;

    if pw == 0 && ph == 0 {
        return Ok(frame.clone().unbind());
    }
    let fb = ZeroCopyBuf::<u8>::extract(frame, "frame")?;
    if fb.shape.as_slice() != [h, w, 3] {
        return Err(PyValueError::new_err("frame shape mismatch"));
    }
    let out = np_empty(py, (gh * s, gw * s, 3usize), "uint8")?;
    let mut out_buf = ZeroCopyMutBuf::<u8>::extract(&out, "padded")?;
    py.allow_threads(|| unsafe {
        s0::pad_into(fb.as_slice(), w, h, gw * s, gh * s, out_buf.as_mut_slice());
    });
    drop(out_buf);
    Ok(out.unbind())
}

fn impl_words_eq(
    py: Python<'_>,
    padded: &Bound<'_, PyAny>,
    words_prev: &Bound<'_, PyAny>,
    grid: &Bound<'_, PyAny>,
) -> PyResult<PyObject> {
    let s: usize = grid.getattr("s")?.extract()?;
    let gw: usize = grid.getattr("gw")?.extract()?;
    let gh: usize = grid.getattr("gh")?.extract()?;
    let n_tiles: usize = grid.getattr("N")?.extract()?;
    if words_prev.is_none() {
        let np = PyModule::import_bound(py, "numpy")?;
        return Ok(np.call_method1("zeros", (n_tiles, "bool"))?.unbind());
    }
    let pb = ZeroCopyBuf::<u8>::extract(padded, "padded")?;
    let wb = ZeroCopyBuf::<u8>::extract(words_prev, "words_prev")?;
    let flat_obj = grid.getattr("flat")?;
    let fb = ZeroCopyBuf::<i64>::extract(&flat_obj, "grid.flat")?;
    let out = np_empty(py, (n_tiles,), "bool")?;
    let mut out_buf = ZeroCopyMutBuf::<u8>::extract(&out, "words_eq")?;
    let eq_vec = py.allow_threads(|| unsafe {
        s0::words_eq(pb.as_slice(), wb.as_slice(), s, gw, gh, fb.as_slice())
    });
    unsafe {
        let dst = out_buf.as_mut_slice();
        for (i, &b) in eq_vec.iter().enumerate() {
            dst[i] = u8::from(b);
        }
    }
    drop(out_buf);
    Ok(out.unbind())
}

fn raise_vqa_error(py: Python<'_>, code: &str, ctx: &Bound<'_, PyDict>) -> PyResult<PyErr> {
    let err_mod = PyModule::import_bound(py, "visioncore.errors")?;
    let vqa_err = err_mod.getattr("VqaError")?;
    let inst = vqa_err.call1((code, ctx))?;
    Ok(PyErr::from_value_bound(inst))
}

fn impl_regions_of(
    py: Python<'_>,
    tiles: &Bound<'_, PyAny>,
    w: i64,
    h: i64,
    frame: &Bound<'_, PyAny>,
) -> PyResult<PyObject> {
    let layout_mod = PyModule::import_bound(py, "visioncore.layout")?;
    let ids_mod = PyModule::import_bound(py, "visioncore.ids")?;
    let gp = layout_mod.call_method1("grid_params", (w, h))?;
    let (s, gw, gh): (u32, u32, u32) = gp.extract()?;
    let ident: String = layout_mod.call_method1("grid_ident", (w, h))?.extract()?;

    let np = PyModule::import_bound(py, "numpy")?;
    let tiles_c = if let Ok(u8_view) = tiles.call_method1("view", ("u1",)) {
        if let Ok(b) = PyBuffer::<u8>::get_bound(&u8_view) {
            if b.is_c_contiguous() {
                u8_view
            } else {
                np.call_method1("ascontiguousarray", (tiles,))?
                    .call_method1("view", ("u1",))?
            }
        } else {
            return Err(PyValueError::new_err("tiles must expose a byte buffer"));
        }
    } else {
        return Err(PyValueError::new_err("tiles must be a numpy array"));
    };

    let tb = ZeroCopyBuf::<u8>::extract(&tiles_c, "tiles")?;
    if tb.len % TILE_SIZE != 0 {
        return Err(PyValueError::new_err(
            "tiles byte length must be a multiple of 32",
        ));
    }

    let raw_res = py.allow_threads(|| unsafe {
        par::regions_of_raw(tb.as_slice(), w as u32, h as u32, s, gw, gh)
    });

    let raw_regions = match raw_res {
        Ok(r) => r,
        Err(bad_m) => {
            let ctx = PyDict::new_bound(py);
            ctx.set_item("morton", bad_m)?;
            return Err(raise_vqa_error(py, "E_VC_ID_RANGE", &ctx)?);
        }
    };

    const GROUP_NAMES: [&str; 3] = ["CHG", "MOV", "FRZ"];
    const CLS_NAMES: [&str; 7] = [
        "UNCHANGED",
        "JITTER",
        "CHANGED",
        "MOVED",
        "FLICKER",
        "FROZEN",
        "DRIFT",
    ];
    const KIND_NAMES: [&str; 4] = ["EDGE", "TEXTURE", "SMOOTH", "MIXED"];

    let out_list = PyList::empty_bound(py);
    for r in raw_regions {
        let reg_id: String = ids_mod
            .call_method1("region_id", (frame, r.min_morton, w, h))?
            .extract()?;
        let d = PyDict::new_bound(py);
        d.set_item("schema", "vqa.core-region/v1")?;
        d.set_item("id", reg_id)?;
        d.set_item("frame", frame)?;
        d.set_item("grid", &ident)?;
        d.set_item("group", GROUP_NAMES[r.group_code as usize])?;
        d.set_item("cls", CLS_NAMES[r.cls_idx])?;
        d.set_item("cls_hist", r.cls_hist.to_vec())?;
        d.set_item("n_tiles", r.n_tiles)?;
        d.set_item("bbox_t", r.bbox_t.to_vec())?;
        d.set_item("bbox_px", r.bbox_px.to_vec())?;
        d.set_item("mv", r.mv.to_vec())?;
        d.set_item("r_max", r.r_max)?;
        d.set_item("r_sum", r.r_sum)?;
        d.set_item("edge_ppm", r.edge_ppm)?;
        d.set_item("kind", KIND_NAMES[r.kind_idx])?;
        d.set_item("shade_only", r.shade_only)?;
        let occ = PyList::empty_bound(py);
        if r.occ_ui {
            occ.append("ui")?;
        }
        if r.occ_actor {
            occ.append("actor")?;
        }
        d.set_item("occ", occ)?;
        let block = PyList::empty_bound(py);
        block.append(r.block_k)?;
        block.append(format!("{:04x}", r.block_b0))?;
        block.append(format!("{:04x}", r.block_b1))?;
        d.set_item("block", block)?;
        let spans = PyList::empty_bound(py);
        for (a, b) in r.spans {
            let pair = PyList::empty_bound(py);
            pair.append(format!("{:04x}", a))?;
            pair.append(format!("{:04x}", b))?;
            spans.append(pair)?;
        }
        d.set_item("spans", spans)?;
        out_list.append(d)?;
    }
    Ok(out_list.into())
}

fn parse_hex_u16(s: &str) -> PyResult<u16> {
    u16::from_str_radix(s, 16).map_err(|_| PyValueError::new_err("invalid hex morton in span"))
}

fn extract_region_spans(reg: &Bound<'_, PyAny>) -> PyResult<(String, Vec<(u16, u16)>)> {
    let id: String = reg.get_item("id")?.extract()?;
    let spans_obj = reg.get_item("spans")?;
    let spans_raw: Vec<Vec<String>> = spans_obj.extract()?;
    let mut spans = Vec::with_capacity(spans_raw.len());
    for pair in spans_raw {
        if pair.len() != 2 {
            return Err(PyValueError::new_err("each span must have 2 elements"));
        }
        spans.push((parse_hex_u16(&pair[0])?, parse_hex_u16(&pair[1])?));
    }
    Ok((id, spans))
}

fn impl_adjacency(py: Python<'_>, regions: &Bound<'_, PyAny>) -> PyResult<PyObject> {
    let seq: Vec<Bound<'_, PyAny>> = regions.extract()?;
    let mut parsed = Vec::with_capacity(seq.len());
    for r in &seq {
        parsed.push(extract_region_spans(r)?);
    }
    let edges = py.allow_threads(|| par::adjacency_from_spans(&parsed));
    let out = PyList::empty_bound(py);
    for (a, b, c) in edges {
        let row = PyList::empty_bound(py);
        row.append(a)?;
        row.append(b)?;
        row.append(c)?;
        out.append(row)?;
    }
    Ok(out.into())
}

fn extract_match_inputs(regions: &[Bound<'_, PyAny>]) -> PyResult<Vec<MatchRegionInput>> {
    let mut out = Vec::with_capacity(regions.len());
    for r in regions {
        let (id, spans) = extract_region_spans(r)?;
        let group: String = r.get_item("group")?.extract()?;
        let mv: [i64; 2] = r.get_item("mv")?.extract()?;
        let n_tiles: usize = r.get_item("n_tiles")?.extract()?;
        out.push(MatchRegionInput {
            id,
            group,
            mv,
            n_tiles,
            spans,
        });
    }
    Ok(out)
}

fn impl_match_regions(
    py: Python<'_>,
    prev: &Bound<'_, PyAny>,
    cur: &Bound<'_, PyAny>,
    global_mv: (i64, i64),
    s: i64,
) -> PyResult<PyObject> {
    let prev_seq: Vec<Bound<'_, PyAny>> = prev.extract()?;
    let cur_seq: Vec<Bound<'_, PyAny>> = cur.extract()?;
    if !prev_seq.is_empty() && !cur_seq.is_empty() {
        let g_prev: String = prev_seq[0].get_item("grid")?.extract()?;
        let g_cur: String = cur_seq[0].get_item("grid")?.extract()?;
        if g_prev != g_cur {
            let ctx = PyDict::new_bound(py);
            ctx.set_item("prev", g_prev)?;
            ctx.set_item("cur", g_cur)?;
            return Err(raise_vqa_error(py, "E_VC_GRID_MISMATCH", &ctx)?);
        }
    }
    let frame: i64 = if !cur_seq.is_empty() {
        cur_seq[0].get_item("frame")?.extract()?
    } else if !prev_seq.is_empty() {
        prev_seq[0].get_item("frame")?.extract::<i64>()? + 1
    } else {
        0
    };
    let prev_in = extract_match_inputs(&prev_seq)?;
    let cur_in = extract_match_inputs(&cur_seq)?;
    let (links, split, merge) =
        py.allow_threads(|| par::match_regions_from_spans(&prev_in, &cur_in, global_mv, s));

    let d = PyDict::new_bound(py);
    d.set_item("schema", "vqa.core-link/v1")?;
    d.set_item("frame", frame)?;
    let links_list = PyList::empty_bound(py);
    for (a, b, ov) in links {
        let row = PyList::empty_bound(py);
        row.append(a)?;
        row.append(b)?;
        row.append(ov)?;
        links_list.append(row)?;
    }
    let split_list = PyList::empty_bound(py);
    for (a, b) in split {
        let row = PyList::empty_bound(py);
        row.append(a)?;
        row.append(b)?;
        split_list.append(row)?;
    }
    let merge_list = PyList::empty_bound(py);
    for (a, b) in merge {
        let row = PyList::empty_bound(py);
        row.append(a)?;
        row.append(b)?;
        merge_list.append(row)?;
    }
    d.set_item("links", links_list)?;
    d.set_item("split", split_list)?;
    d.set_item("merge", merge_list)?;
    Ok(d.into())
}

fn impl_pool_info(py: Python<'_>) -> PyResult<PyObject> {
    let (configured, active, worker_name) = py.allow_threads(par::pool_status);
    let d = PyDict::new_bound(py);
    d.set_item("configured_threads", configured)?;
    d.set_item("active_threads", active)?;
    d.set_item("worker_name", worker_name)?;
    d.set_item("serial", configured <= 1)?;
    Ok(d.into())
}

// ============================================================================
// Guarded #[pyfunction] entrypoints (statically checked by test_rt1_native_build.py)
// ============================================================================

#[pyfunction]
fn abi_version() -> PyResult<u32> {
    guard("abi_version", || Ok(ABI_VERSION))
}

#[pyfunction]
fn pool_info(py: Python<'_>) -> PyResult<PyObject> {
    guard("pool_info", || impl_pool_info(py))
}

#[pyfunction]
#[pyo3(signature = (n=None))]
fn set_threads(n: Option<usize>) -> PyResult<()> {
    guard("set_threads", || {
        par::set_threads_override(n).map_err(PyValueError::new_err)
    })
}

#[pyfunction]
fn get_threads() -> PyResult<usize> {
    guard("get_threads", || Ok(par::configured_threads()))
}

/// Echo the compiled TILE_DTYPE: {"itemsize": int, "fields": [(name, offset, size, kind), ...]}.
/// With dtype_descr = {"itemsize": int, "fields": [(name, offset, size, kind), ...]} (kind = numpy dtype.kind, "u" or "i")
/// it raises ValueError on any difference in itemsize, name, offset, size or signedness.
#[pyfunction]
#[pyo3(signature = (dtype_descr=None))]
fn layout_check(py: Python<'_>, dtype_descr: Option<&Bound<'_, PyDict>>) -> PyResult<PyObject> {
    guard("layout_check", || {
        if let Some(descr) = dtype_descr {
            let isz: usize = match descr.get_item("itemsize")? {
                Some(v) => v.extract()?,
                None => return Err(PyValueError::new_err("dtype_descr lacks itemsize")),
            };
            let fields: Vec<(String, usize, usize, char)> = match descr.get_item("fields")? {
                Some(v) => v.extract()?,
                None => return Err(PyValueError::new_err("dtype_descr lacks fields")),
            };
            let same = isz == TILE_SIZE
                && fields.len() == TILE_FIELDS.len()
                && fields
                    .iter()
                    .zip(TILE_FIELDS.iter())
                    .all(|(a, b)| a.0 == b.0 && a.1 == b.1 && a.2 == b.2 && a.3 == b.3);
            if !same {
                return Err(PyValueError::new_err(
                    "layout mismatch: compiled TILE_FIELDS differ from dtype_descr",
                ));
            }
        }
        let d = PyDict::new_bound(py);
        d.set_item("itemsize", TILE_SIZE)?;
        let l = PyList::empty_bound(py);
        for (n, o, s, k) in TILE_FIELDS.iter() {
            l.append((*n, *o, *s, k.to_string()))?;
        }
        d.set_item("fields", l)?;
        Ok(d.into())
    })
}

/// Panics inside guard() and returns RuntimeError: proves a Rust panic never aborts the interpreter.
#[pyfunction]
fn panic_probe() -> PyResult<()> {
    guard("panic_probe", || {
        if ABI_VERSION != u32::MAX {
            panic!("panic_probe: deliberate");
        }
        Ok(())
    })
}

#[pyfunction]
#[pyo3(signature = (name=None))]
fn set_mutant(name: Option<&str>) -> PyResult<()> {
    guard("set_mutant", || {
        set_mutant_by_name(name).map_err(PyValueError::new_err)
    })
}

#[pyfunction]
fn get_mutant() -> PyResult<Option<&'static str>> {
    guard("get_mutant", || Ok(active_mutant_name()))
}

#[pyfunction]
fn _tile_sad_windows(
    py: Python<'_>,
    y_cur: &Bound<'_, PyAny>,
    y_prev: &Bound<'_, PyAny>,
    c: (i64, i64),
    s: usize,
    tx: &Bound<'_, PyAny>,
    ty: &Bound<'_, PyAny>,
) -> PyResult<PyObject> {
    guard("_tile_sad_windows", || {
        impl_tile_sad_windows(py, y_cur, y_prev, c, s, tx, ty)
    })
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn _tile_sad(
    py: Python<'_>,
    y_cur: &Bound<'_, PyAny>,
    y_prev: &Bound<'_, PyAny>,
    c: (i64, i64),
    s: usize,
    gh: usize,
    gw: usize,
    tx: &Bound<'_, PyAny>,
    ty: &Bound<'_, PyAny>,
) -> PyResult<PyObject> {
    guard("_tile_sad", || {
        impl_tile_sad(py, y_cur, y_prev, c, s, gh, gw, tx, ty)
    })
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn _tile_sad_clip(
    py: Python<'_>,
    y_cur: &Bound<'_, PyAny>,
    y_prev: &Bound<'_, PyAny>,
    c: (i64, i64),
    s: usize,
    gh: usize,
    gw: usize,
    tx: &Bound<'_, PyAny>,
    ty: &Bound<'_, PyAny>,
) -> PyResult<PyObject> {
    guard("_tile_sad_clip", || {
        impl_tile_sad_clip(py, y_cur, y_prev, c, s, gh, gw, tx, ty)
    })
}

#[pyfunction]
fn _axis_shift(
    py: Python<'_>,
    cur: &Bound<'_, PyAny>,
    prev: &Bound<'_, PyAny>,
    dmax: i64,
) -> PyResult<i64> {
    guard("_axis_shift", || impl_axis_shift(py, cur, prev, dmax))
}

#[pyfunction]
fn global_vector(
    py: Python<'_>,
    y_cur: &Bound<'_, PyAny>,
    y_prev: &Bound<'_, PyAny>,
    s: i64,
) -> PyResult<(i64, i64)> {
    guard("global_vector", || impl_global_vector(py, y_cur, y_prev, s))
}

#[pyfunction]
fn _tile_sums(
    py: Python<'_>,
    absdiff: &Bound<'_, PyAny>,
    s: usize,
    gh: usize,
    gw: usize,
) -> PyResult<PyObject> {
    guard("_tile_sums", || impl_tile_sums(py, absdiff, s, gh, gw))
}

#[pyfunction]
fn _islands(py: Python<'_>, arg: &Bound<'_, PyAny>) -> PyResult<PyObject> {
    guard("_islands", || impl_islands(py, arg))
}

#[pyfunction]
fn s4_islands(py: Python<'_>, cells: &Bound<'_, PyDict>) -> PyResult<PyObject> {
    guard("s4_islands", || impl_s4_islands(py, cells))
}

#[pyfunction]
fn island_vector(
    py: Python<'_>,
    lab_mask: &Bound<'_, PyAny>,
    y_cur: &Bound<'_, PyAny>,
    y_prev: &Bound<'_, PyAny>,
    s: usize,
) -> PyResult<(i64, i64)> {
    guard("island_vector", || {
        impl_island_vector(py, lab_mask, y_cur, y_prev, s)
    })
}

#[pyfunction]
fn luma_of_padded(py: Python<'_>, padded: &Bound<'_, PyAny>) -> PyResult<PyObject> {
    guard("luma_of_padded", || impl_luma_of_padded(py, padded))
}

#[pyfunction]
fn ycocg(py: Python<'_>, rgb: &Bound<'_, PyAny>) -> PyResult<PyObject> {
    guard("ycocg", || impl_ycocg(py, rgb))
}

#[pyfunction]
fn isqrt(py: Python<'_>, v: &Bound<'_, PyAny>) -> PyResult<PyObject> {
    guard("isqrt", || impl_isqrt(py, v))
}

#[pyfunction]
fn _compute(
    py: Python<'_>,
    out: &Bound<'_, PyAny>,
    idx: &Bound<'_, PyAny>,
    padded: &Bound<'_, PyAny>,
    grid: &Bound<'_, PyAny>,
    p: &Bound<'_, PyAny>,
    y_plane: &Bound<'_, PyAny>,
) -> PyResult<()> {
    guard("_compute", || {
        impl_compute(py, out, idx, padded, grid, p, y_plane)
    })
}

#[pyfunction]
fn describe_padded_tiles(
    py: Python<'_>,
    padded: &Bound<'_, PyAny>,
    grid: &Bound<'_, PyAny>,
) -> PyResult<PyObject> {
    guard("describe_padded_tiles", || {
        impl_describe_padded_tiles(py, padded, grid)
    })
}

#[pyfunction]
#[pyo3(signature = (grid_or_frame, frame=None))]
fn pad(
    py: Python<'_>,
    grid_or_frame: &Bound<'_, PyAny>,
    frame: Option<&Bound<'_, PyAny>>,
) -> PyResult<PyObject> {
    guard("pad", || impl_pad(py, grid_or_frame, frame))
}

#[pyfunction]
fn words_eq(
    py: Python<'_>,
    padded: &Bound<'_, PyAny>,
    words_prev: &Bound<'_, PyAny>,
    grid: &Bound<'_, PyAny>,
) -> PyResult<PyObject> {
    guard("words_eq", || impl_words_eq(py, padded, words_prev, grid))
}

#[pyfunction]
fn regions_of(
    py: Python<'_>,
    tiles: &Bound<'_, PyAny>,
    w: i64,
    h: i64,
    frame: &Bound<'_, PyAny>,
) -> PyResult<PyObject> {
    guard("regions_of", || impl_regions_of(py, tiles, w, h, frame))
}

#[pyfunction]
fn adjacency(py: Python<'_>, regions: &Bound<'_, PyAny>) -> PyResult<PyObject> {
    guard("adjacency", || impl_adjacency(py, regions))
}

#[pyfunction]
#[pyo3(signature = (prev, cur, global_mv=(0, 0), s=16))]
fn match_regions(
    py: Python<'_>,
    prev: &Bound<'_, PyAny>,
    cur: &Bound<'_, PyAny>,
    global_mv: (i64, i64),
    s: i64,
) -> PyResult<PyObject> {
    guard("match_regions", || {
        impl_match_regions(py, prev, cur, global_mv, s)
    })
}

#[pymodule]
fn visioncore_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(abi_version, m)?)?;
    m.add_function(wrap_pyfunction!(pool_info, m)?)?;
    m.add_function(wrap_pyfunction!(set_threads, m)?)?;
    m.add_function(wrap_pyfunction!(get_threads, m)?)?;
    m.add_function(wrap_pyfunction!(layout_check, m)?)?;
    m.add_function(wrap_pyfunction!(panic_probe, m)?)?;
    m.add_function(wrap_pyfunction!(set_mutant, m)?)?;
    m.add_function(wrap_pyfunction!(get_mutant, m)?)?;
    m.add_function(wrap_pyfunction!(_tile_sad_windows, m)?)?;
    m.add_function(wrap_pyfunction!(_tile_sad, m)?)?;
    m.add_function(wrap_pyfunction!(_tile_sad_clip, m)?)?;
    m.add_function(wrap_pyfunction!(_axis_shift, m)?)?;
    m.add_function(wrap_pyfunction!(global_vector, m)?)?;
    m.add_function(wrap_pyfunction!(_tile_sums, m)?)?;
    m.add_function(wrap_pyfunction!(_islands, m)?)?;
    m.add_function(wrap_pyfunction!(s4_islands, m)?)?;
    m.add_function(wrap_pyfunction!(island_vector, m)?)?;
    m.add_function(wrap_pyfunction!(luma_of_padded, m)?)?;
    m.add_function(wrap_pyfunction!(ycocg, m)?)?;
    m.add_function(wrap_pyfunction!(isqrt, m)?)?;
    m.add_function(wrap_pyfunction!(_compute, m)?)?;
    m.add_function(wrap_pyfunction!(describe_padded_tiles, m)?)?;
    m.add_function(wrap_pyfunction!(pad, m)?)?;
    m.add_function(wrap_pyfunction!(words_eq, m)?)?;
    m.add_function(wrap_pyfunction!(regions_of, m)?)?;
    m.add_function(wrap_pyfunction!(adjacency, m)?)?;
    m.add_function(wrap_pyfunction!(match_regions, m)?)?;
    Ok(())
}

#[pymodule]
fn native_parity_candidate(m: &Bound<'_, PyModule>) -> PyResult<()> {
    visioncore_native(m)
}
