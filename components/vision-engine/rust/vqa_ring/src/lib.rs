#![deny(unsafe_op_in_unsafe_fn)]
//! Linux sealed-memfd single-writer frame ring. All shared payload accesses are
//! atomic bytes: the seqlock detects inconsistent copies without Rust data races.
#[cfg(not(all(target_os = "linux", target_endian = "little")))]
compile_error!("vqa_ring requires little-endian Linux");
pub mod error;
pub mod handover;
pub mod layout;
mod map;
pub mod notify;
pub mod reader;
pub mod writer;
pub use error::{Result, RingError};
pub use layout::{Format, Geometry};
pub use reader::{Frame, ReadResult, Reader};
pub use writer::Writer;

#[cfg(test)]
mod tests;
