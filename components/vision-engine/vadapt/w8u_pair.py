"""W8U's offline RGB24 pair validator (no X11 or wire-transport authority).

The producer is an injected srf-fbread-style capability. Only a separately
injected verifier may attest grab/ungrab completion. No X11 session is started.
"""

from dataclasses import dataclass
import struct

from vadapt import xfb

RESIDENT_PAIR = "resident_pair"
UNSYNCHRONISED_CONSISTENT = "unsynchronised_consistent"
_CAPTURE_MODES = frozenset((RESIDENT_PAIR, UNSYNCHRONISED_CONSISTENT))
_WIRE_OVERHEAD = 128 + 16
_GEOMETRY_SLACK = 1 << 20


def select_capture_mode(mode):
    """Select one mode exactly; an invalid or rejected pair has no fallback."""
    if type(mode) is not str or mode not in _CAPTURE_MODES:
        raise xfb.XwdError("E_WIRE", "unrecognised capture mode")
    return mode


@dataclass(frozen=True)
class PairBounds:
    """Explicit per-session limits; values must originate in an accepted owner.

    No protocol XWD metadata caps are invented here. A caller must inject all
    negotiated or frozen limits; these are not wire schema definitions.
    """

    max_header_bytes: int
    max_colors: int
    max_stride_bytes: int
    max_record_bytes: int

    def __post_init__(self):
        for name in ("max_header_bytes", "max_stride_bytes", "max_record_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise xfb.XwdError("E_LENGTH", "%s must be a positive exact integer" % name)
        if type(self.max_colors) is not int or self.max_colors < 0:
            raise xfb.XwdError("E_LENGTH", "max_colors must be a nonnegative exact integer")


@dataclass(frozen=True)
class RGBEvidence:
    """Owned top-down RGB24 and canonical hashes, never raw XWD or wire bytes."""

    rgb24: bytes
    width: int
    height: int
    stride: int
    sha256: str
    framebuffer_sha256: str
    getimage_sha256: str
    mode: str = RESIDENT_PAIR


@dataclass(frozen=True)
class PairSample:
    """Two XWD snapshots from the *external* producer and its opaque receipt."""

    framebuffer_xwd: bytes
    getimage_xwd: bytes
    sender_completion: object
    advertised_sha256: object = None


@dataclass(frozen=True)
class VerifiedGrab:
    """Result of an independently injected, trusted completion verifier.

    A producer's grabbed flag is not evidence. This object must be returned by
    the trusted verifier after it checks an opaque sender-side completion.
    """

    acquired_mono_ns: int
    released_mono_ns: int
    deadline_mono_ns: int
    held_during_both_legs: bool
    ungrab_succeeded: bool


def _geometry(expected_geometry):
    if (type(expected_geometry) is not tuple or len(expected_geometry) != 2
            or any(type(side) is not int or not (0 < side <= xfb.MAX_SIDE)
                   for side in expected_geometry)):
        raise xfb.XwdError("E_GEOMETRY", "expected_geometry must be (positive exact int, positive exact int)")
    return expected_geometry


def _raw_size(buf):
    if not isinstance(buf, (bytes, bytearray, memoryview)):
        raise xfb.XwdError("E_WIRE", "XWD leg must be a byte buffer")
    return buf.nbytes if isinstance(buf, memoryview) else len(buf)


def _validate_xwd_leg(raw, geometry, bounds):
    """Inspect the untrusted XWD metadata before owned RGB24 allocation."""
    size = _raw_size(raw)
    if size < xfb.MIN_HEADER:
        raise xfb.XwdError("E_LENGTH", "XWD shorter than fixed header")
    # The record-size check has already occurred. Copy mutable bytes only now.
    raw = bytes(raw)
    try:
        fields = struct.unpack_from(">25I", raw)
    except struct.error as exc:
        raise xfb.XwdError("E_LENGTH", "invalid XWD header") from exc
    header_bytes, width, height = fields[0], fields[4], fields[5]
    stride, ncolors = fields[12], fields[19]
    if (header_bytes < xfb.MIN_HEADER or header_bytes > bounds.max_header_bytes
            or ncolors > bounds.max_colors or stride > bounds.max_stride_bytes):
        raise xfb.XwdError("E_LENGTH", "XWD metadata exceeds explicit limits")
    if (width, height) != geometry:
        raise xfb.XwdError("E_GEOMETRY", "leg geometry does not match expected_geometry")
    if header_bytes + ncolors * xfb.COLOR_ENTRY + height * stride != size:
        raise xfb.XwdError("E_LENGTH", "XWD exact total length mismatch")
    hd = xfb.validate_frame(raw, width=geometry[0], height=geometry[1])
    rgb = xfb.rgb24(raw, hd)
    digest = xfb.frame_sha256(raw, hd)
    return rgb, digest


def _advertised_hex(value):
    if type(value) is bytes and len(value) == 32:
        return value.hex()
    if (type(value) is str and len(value) == 64
            and all(ch in "0123456789abcdef" for ch in value)):
        return value
    raise xfb.XwdError("E_HASH", "advertised SHA-256 must be 32 raw bytes or 64 lowercase hex digits")


def validate_pair(framebuffer_xwd, getimage_xwd, *, expected_geometry,
                  bounds, advertised_sha256=None):
    """Admit only canonical-RGB24-equal XWD legs; return immutable owned evidence.

    This pure validator does *not* attest a grab. Use capture_resident_pair to
    require independent checked sender completion before any publication.
    """
    geometry = _geometry(expected_geometry)
    if not isinstance(bounds, PairBounds):
        raise xfb.XwdError("E_LENGTH", "explicit PairBounds required")
    count_a = _raw_size(framebuffer_xwd)
    count_b = _raw_size(getimage_xwd)
    total = _WIRE_OVERHEAD + count_a + count_b
    geometry_cap = 2 * (4 * geometry[0] * geometry[1] + _GEOMETRY_SLACK) + _WIRE_OVERHEAD
    if total > min(bounds.max_record_bytes, geometry_cap):
        raise xfb.XwdError("E_LENGTH", "pair exceeds negotiated or geometry record cap")
    rgb_a, sha_a = _validate_xwd_leg(framebuffer_xwd, geometry, bounds)
    rgb_b, sha_b = _validate_xwd_leg(getimage_xwd, geometry, bounds)
    if rgb_a != rgb_b or sha_a != sha_b:
        raise xfb.XwdError("E_TORN", "grab-held legs have different canonical RGB24 pixels")
    if advertised_sha256 is not None and _advertised_hex(advertised_sha256) != sha_a:
        raise xfb.XwdError("E_HASH", "advertised SHA-256 differs from RGB24")
    return RGBEvidence(rgb_a, geometry[0], geometry[1], geometry[0] * 3,
                       sha_a, sha_a, sha_b)


def _verify_grab(receipt, verify_sender_completion):
    if not callable(verify_sender_completion):
        raise xfb.XwdError("E_WIRE", "independent sender completion verifier required")
    checked = verify_sender_completion(receipt)
    if not isinstance(checked, VerifiedGrab):
        raise xfb.XwdError("E_WIRE", "a flag alone cannot attest grab completion")
    for name in ("acquired_mono_ns", "released_mono_ns", "deadline_mono_ns"):
        value = getattr(checked, name)
        if type(value) is not int or value < 0:
            raise xfb.XwdError("E_TIMEOUT", "invalid verified grab clock")
    if type(checked.held_during_both_legs) is not bool or not checked.held_during_both_legs:
        raise xfb.XwdError("E_TORN", "verified grab did not cover both legs")
    if type(checked.ungrab_succeeded) is not bool or not checked.ungrab_succeeded:
        raise xfb.XwdError("E_TIMEOUT", "ungrab not confirmed")
    if not (checked.acquired_mono_ns <= checked.released_mono_ns <= checked.deadline_mono_ns):
        raise xfb.XwdError("E_TIMEOUT", "grab expired before confirmed release")


def capture_resident_pair(producer, *, expected_geometry, bounds,
                          verify_sender_completion, publish, check_cancelled=None):
    """Collect two external legs; verify release, pixels and cancellation, then publish.

    Never attempts unsynchronised_consistent after a failed resident pair.
    ``publish`` is the caller's atomic admission boundary; if any prior check
    fails, it is never called. Producer and verifier are injected capabilities.
    """
    select_capture_mode(RESIDENT_PAIR)
    if not callable(producer) or not callable(publish):
        raise xfb.XwdError("E_WIRE", "producer and atomic publisher required")
    if not callable(verify_sender_completion):
        raise xfb.XwdError("E_WIRE", "independent sender verifier required")
    sample = producer()
    if not isinstance(sample, PairSample):
        raise xfb.XwdError("E_WIRE", "producer must supply PairSample")
    _verify_grab(sample.sender_completion, verify_sender_completion)
    if sample.advertised_sha256 is None:
        raise xfb.XwdError("E_HASH", "resident pair must advertise a canonical RGB24 SHA-256")
    evidence = validate_pair(sample.framebuffer_xwd, sample.getimage_xwd,
                             expected_geometry=expected_geometry, bounds=bounds,
                             advertised_sha256=sample.advertised_sha256)
    if check_cancelled is not None:
        if not callable(check_cancelled):
            raise xfb.XwdError("E_WIRE", "check_cancelled must be callable")
        if check_cancelled() is True:
            raise xfb.XwdError("E_TIMEOUT", "capture cancelled before publication")
    return publish(evidence)


def capture_unsynchronised_consistent(read_fn, *, expected_geometry, bounds,
                                      tries=xfb.TORN_TRIES):
    """Explicit legacy fallback: retain xfb.consistent_read's bounded E_TORN."""
    geometry = _geometry(expected_geometry)
    if not isinstance(bounds, PairBounds):
        raise xfb.XwdError("E_LENGTH", "explicit PairBounds required")
    # This mode is never selected automatically on resident_pair failure.
    single_leg_cap = 4 * geometry[0] * geometry[1] + _GEOMETRY_SLACK
    raw = xfb.consistent_read(read_fn, tries=tries,
                              max_bytes=min(bounds.max_record_bytes, single_leg_cap))
    rgb, sha = _validate_xwd_leg(raw, geometry, bounds)
    return RGBEvidence(rgb, geometry[0], geometry[1], geometry[0] * 3,
                       sha, sha, sha, UNSYNCHRONISED_CONSISTENT)
