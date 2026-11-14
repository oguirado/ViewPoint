//! A connected AF_UNIX SOCK_SEQPACKET accepted from a filesystem listener is
//! required. The application owns listener ACLs, accept/connect deadlines and
//! ramws adoption. Never use an inherited socketpair across identity changes.
use crate::{
    layout::{Format, Geometry},
    map, Reader, Result, RingError, Writer,
};
use std::os::fd::{AsRawFd, BorrowedFd, OwnedFd};
#[derive(Clone, Copy, Debug)]
pub struct Handshake {
    pub geometry: Geometry,
    pub pid: u32,
    pub nonce: u64,
}
impl Handshake {
    /// Packed LE: magic@0, bytes@8, slots@16, width@20, height@24,
    /// fmt@28, pid@32, nonce@36, zero padding@44..64.
    pub fn encode(&self) -> [u8; 64] {
        let mut b = [0; 64];
        b[..8].copy_from_slice(b"VQAHAND3");
        b[8..16].copy_from_slice(&(self.geometry.ring_bytes as u64).to_le_bytes());
        for (o, v) in [
            (16, self.geometry.slots),
            (20, self.geometry.width),
            (24, self.geometry.height),
            (28, self.geometry.fmt as u32),
            (32, self.pid),
        ] {
            b[o..o + 4].copy_from_slice(&v.to_le_bytes())
        }
        b[36..44].copy_from_slice(&self.nonce.to_le_bytes());
        b
    }
    pub fn decode(b: &[u8; 64]) -> Result<Self> {
        if &b[..8] != b"VQAHAND3" || b[44..].iter().any(|&v| v != 0) {
            return Err(RingError::Format);
        }
        let u = |o| u32::from_le_bytes(b[o..o + 4].try_into().unwrap());
        let g = Geometry::new(u(16), u(20), u(24), Format::try_from(u(28))?)?;
        if u64::from_le_bytes(b[8..16].try_into().unwrap()) != g.ring_bytes as u64 {
            return Err(RingError::Size);
        }
        Ok(Self {
            geometry: g,
            pid: u(32),
            nonce: u64::from_le_bytes(b[36..44].try_into().unwrap()),
        })
    }
}
#[derive(Debug)]
pub struct Policy<'a> {
    pub allow_uids: &'a [u32],
    pub expected_nonce: Option<u64>,
}
pub fn send(sock: BorrowedFd<'_>, writer: &Writer, host_uids: &[u32], nonce: u64) -> Result<()> {
    map::check_socket(sock.as_raw_fd())?;
    let p = map::peer(sock.as_raw_fd())?;
    if !host_uids.contains(&p.uid) {
        return Err(RingError::Peer);
    }
    let h = Handshake {
        geometry: writer.geometry,
        pid: std::process::id(),
        nonce,
    };
    map::send(sock.as_raw_fd(), &h.encode(), &[writer.fd.as_raw_fd()])
}
pub fn recv(sock: BorrowedFd<'_>, policy: &Policy<'_>) -> Result<(Reader, Handshake)> {
    map::check_socket(sock.as_raw_fd())?;
    // Receive ownership first: every rejection below drops all received rights.
    let (bytes, fd) = map::recv(sock.as_raw_fd())?;
    let peer = map::peer(sock.as_raw_fd())?;
    if !policy.allow_uids.contains(&peer.uid) {
        return Err(RingError::Peer);
    }
    let h = Handshake::decode(&bytes)?;
    if h.pid != peer.pid as u32 || policy.expected_nonce.is_some_and(|n| n != h.nonce) {
        return Err(RingError::Peer);
    }
    verify_fd(fd, h, peer.uid).map(|r| (r, h))
}
pub fn verify_fd(fd: OwnedFd, h: Handshake, peer_uid: u32) -> Result<Reader> {
    map::check_seals(fd.as_raw_fd())?;
    let s = map::stat(fd.as_raw_fd())?;
    if s.st_uid != peer_uid {
        return Err(RingError::Peer);
    }
    if s.st_size != h.geometry.ring_bytes as i64 {
        return Err(RingError::Size);
    }
    Reader::from_fd(fd, h.geometry)
}
