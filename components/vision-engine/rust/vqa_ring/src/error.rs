use std::{fmt, io};
#[derive(Debug)]
pub enum RingError {
    Unsealed,
    Peer,
    Size,
    Format,
    Ctrunc,
    NotMemfd,
    Io(io::Error),
}
pub type Result<T> = std::result::Result<T, RingError>;
impl RingError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::Unsealed => "E_RING_UNSEALED",
            Self::Peer => "E_RING_PEER",
            Self::Size => "E_RING_SIZE",
            Self::Format => "E_RING_FORMAT",
            Self::Ctrunc => "E_RING_CTRUNC",
            Self::NotMemfd => "E_RING_NOTMEMFD",
            Self::Io(_) => "E_RING_IO",
        }
    }
}
impl fmt::Display for RingError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.code())
    }
}
impl std::error::Error for RingError {}
impl From<io::Error> for RingError {
    fn from(e: io::Error) -> Self {
        Self::Io(e)
    }
}
