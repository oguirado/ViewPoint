"""Proto1 B-pair: fail-closed, immutable RGB24 admission into a real stream bank.

Owned public API (new in this adapter, not a pre-existing protocol schema)::

    parse_pair_payload(payload, *, bounds: w8u_pair.PairBounds) -> (bytes, bytes)
    expected_run_ram_root(run_id: str) -> str   # *path only*, not RAM proof
    PairAuditor(*, run_id: str, target_handle: window_stream_bank.Handle,
                expected_geometry: tuple[int, int], bounds: w8u_pair.PairBounds,
                bank: window_stream_bank.WindowStreamBank,
                bank_spec: window_stream_bank.StreamSpec,
                verify_sender_completion: Callable[[object], w8u_pair.VerifiedGrab],
                verify_collection: Callable[[object], VerifiedCollection] | None,
                max_skew_ns: int,
                check_cancelled: Callable[[], bool] | None = None)
    PairAuditor.accept(payload, *, advertised_rgb24_sha256: str | bytes,
                        sender_completion: object) -> AuditedFrame
    PairAuditor.check_bank_frame(ref: window_stream_bank.FrameRef,
                                 admitted: AuditedFrame) -> FrameIdentity

The two length-prefixed XWD legs describe ONE selected root/full-screen window.
Neither matching pixels nor a sender flag proves origin. The caller MUST inject
separately trusted grab and collection verifiers for the opaque sender receipt;
no verifier means refusal. ``VerifiedCollection`` attests one selected target,
full-screen collection, and bounded leg times inside a verified held grab.
Its time limit is owner-configured; this module does not infer measurements.

The bank owns an in-memory ring reservation; its first allocation and publish
occur only after all pair, grab, collection, skew, and identity checks. The bank
must have one writer per handle and no outside producer for this token. ACK
credit is an accounting entitlement, NOT a sent transport message. Physical
RAM residency, live image/profile, session-clock qualification, and cleanup
capability remain separate conductor/os-cu-0 gates; a /dev/shm path alone does
not attest any of them. No seat-schema field is introduced by this module.
"""

from collections import deque
from dataclasses import dataclass
import hashlib
import json
import re
import struct
import threading

import numpy as np

from vadapt import w8u_pair, xfb
from vadapt.window_stream_bank import (Drop, FrameRef, Handle, StreamError,
                                       StreamSpec, StreamToken, WindowStreamBank)

_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z", re.ASCII)
_HEX64 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_U64 = struct.Struct(">Q")
_RAM_ROOT = "/dev/shm/vqa-ramws"
_MAX_SEQ = (1 << 64) - 2


class PairAuditRefusal(ValueError):
    """Typed refusal. ``__cause__`` retains the original XwdError/StreamError."""

    def __init__(self, code: str, message: str):
        super().__init__("%s %s" % (code, message))
        self.code = code


def _refuse(code, message):
    raise PairAuditRefusal(code, message)


def _positive(value, name, *, zero_ok=False, maximum=None):
    if (type(value) is not int or value < (0 if zero_ok else 1)
            or (maximum is not None and value > maximum)):
        _refuse("E_BINDING", "%s must be an exact integer in range" % name)
    return value


def _digest(value, name):
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        _refuse("E_HASH", "%s must be 64 lowercase SHA-256 hex digits" % name)
    return value


def _run_id(value):
    if (type(value) is not str or _RUN_ID.fullmatch(value) is None
            or value in (".", "..") or ".." in value):
        _refuse("E_BINDING", "invalid run_id")
    return value


def expected_run_ram_root(run_id: str) -> str:
    """Return an expected path, NEVER a statement that this path is real tmpfs."""
    return "%s/opensuperficies/os-cu-0/%s" % (_RAM_ROOT, _run_id(run_id))


def parse_pair_payload(payload, *, bounds: w8u_pair.PairBounds) -> tuple[bytes, bytes]:
    """Decode [u64be length, XWD_A, u64be length, XWD_B] with bounds BEFORE copying.

    Returns independently owned, immutable legs. Raises E_LENGTH for framing,
    including overflow-looking lengths, trailing data and noncontiguous input.
    This is not a serializer and does not make any sender-provenance claim.
    """
    if not isinstance(bounds, w8u_pair.PairBounds):
        _refuse("E_LENGTH", "explicit PairBounds required")
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        _refuse("E_LENGTH", "pair payload must be bytes-like")
    view = memoryview(payload)
    try:
        if not view.c_contiguous:
            _refuse("E_LENGTH", "pair payload must be contiguous")
        view = view.cast("B")
        size = view.nbytes
        if size > bounds.max_record_bytes or size < 16 + 2 * xfb.MIN_HEADER:
            _refuse("E_LENGTH", "pair exceeds negotiated cap or is too short")
        len_a = _U64.unpack_from(view, 0)[0]
        # Size bounds are checked before allocating either leg.
        if len_a < xfb.MIN_HEADER or len_a > size - 16 - xfb.MIN_HEADER:
            _refuse("E_LENGTH", "first XWD length outside available payload")
        second_start = 8 + len_a
        len_b = _U64.unpack_from(view, second_start)[0]
        if len_b < xfb.MIN_HEADER or len_b != size - (second_start + 8):
            _refuse("E_LENGTH", "second XWD length outside exact payload")
        a = bytes(view[8:second_start])
        b = bytes(view[second_start + 8:])
        return a, b
    finally:
        view.release()


@dataclass(frozen=True)
class FrameIdentity:
    """Exact field order defined by the B-pair contract."""

    frame_seq: int
    width: int
    height: int
    rgb24_sha256: str
    binding_sha256: str

    def __post_init__(self):
        _positive(self.frame_seq, "frame_seq", maximum=_MAX_SEQ)
        _positive(self.width, "width", maximum=xfb.MAX_SIDE)
        _positive(self.height, "height", maximum=xfb.MAX_SIDE)
        _digest(self.rgb24_sha256, "rgb24_sha256")
        _digest(self.binding_sha256, "binding_sha256")


@dataclass(frozen=True)
class VerifiedCollection:
    """Independently verified metadata for the SAME opaque sender receipt.

    Times are monotonic ns for the first and second leg's observations; they
    must be within VerifiedGrab's held interval and max_skew_ns. These fields
    are NOT claims from the untrusted XWD header or any per-display sequence.
    """

    target_handle: Handle
    first_leg_mono_ns: int
    second_leg_mono_ns: int
    collected_both_legs: bool
    same_selected_root: bool
    root_full_screen: bool


@dataclass(frozen=True)
class AuditedFrame:
    """Successful owned pixels and identity. No transport ACK was sent."""

    identity: FrameIdentity
    rgb24: bytes
    stride: int
    run_id: str
    target_handle: Handle
    epoch: int
    mode: str
    ack_credit: int

    def __post_init__(self):
        if type(self.rgb24) is not bytes:
            _refuse("E_BINDING", "admitted RGB24 must be owned bytes")
        if self.stride != self.identity.width * 3:
            _refuse("E_GEOMETRY", "canonical RGB24 stride mismatch")
        if len(self.rgb24) != self.stride * self.identity.height:
            _refuse("E_GEOMETRY", "canonical RGB24 size mismatch")
        if hashlib.sha256(self.rgb24).hexdigest() != self.identity.rgb24_sha256:
            _refuse("E_HASH", "owned RGB24 digest mismatch")
        _run_id(self.run_id)
        if not isinstance(self.target_handle, Handle):
            _refuse("E_BINDING", "target_handle must be structural Handle")
        _positive(self.epoch, "epoch", zero_ok=True)
        if self.mode != w8u_pair.RESIDENT_PAIR or type(self.ack_credit) is not int or self.ack_credit != 1:
            _refuse("E_BINDING", "nonresident frame or invalid ACK entitlement")
        expected_binding = _binding_sha256(
            self.run_id, StreamToken(self.target_handle, self.epoch),
            self.identity.frame_seq, self.identity.width, self.identity.height,
            self.identity.rgb24_sha256)
        if self.identity.binding_sha256 != expected_binding:
            _refuse("E_BINDING", "frame binding digest disagrees with owner fields")


def _binding_sha256(run_id, token, seq, width, height, digest):
    # JSON is canonicalized explicitly and includes complete structural owner.
    value = {"run_id": run_id, "target_handle": {
        "kind": token.handle.kind,
        "canonical_id": token.handle.canonical_id,
        "generation": token.handle.generation},
        "epoch": token.epoch, "frame_seq": seq, "width": width,
        "height": height, "rgb24_sha256": digest}
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True,
                     separators=(",", ":")).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def _require_collection(value, grab, *, target_handle, max_skew_ns):
    if not isinstance(value, VerifiedCollection):
        _refuse("E_PROVENANCE", "independent collection evidence is absent")
    if (value.target_handle != target_handle or type(value.collected_both_legs) is not bool
            or not value.collected_both_legs or type(value.same_selected_root) is not bool
            or not value.same_selected_root or type(value.root_full_screen) is not bool
            or not value.root_full_screen):
        _refuse("E_PROVENANCE", "collection not bound to same selected full-screen root")
    first, second = value.first_leg_mono_ns, value.second_leg_mono_ns
    if (type(first) is not int or type(second) is not int
            or first < 0 or second < 0):
        _refuse("E_PROVENANCE", "collection measurement unknown or invalid")
    if not (grab.acquired_mono_ns <= first <= second <= grab.released_mono_ns):
        _refuse("E_PROVENANCE", "leg collection not inside the verified grab interval")
    if second - first > max_skew_ns:
        _refuse("E_SKEW", "verified leg skew exceeds owner limit")


def _translated_xwd_code(code):
    if code in {"E_XWD_GEOMETRY"}:
        return "E_GEOMETRY"
    if code in {"E_XWD_SHORT", "E_XWD_SIZE", "E_XWD_LIMIT"}:
        return "E_LENGTH"
    if code in {"E_TORN", "E_HASH", "E_LENGTH", "E_GEOMETRY", "E_TIMEOUT", "E_WIRE"}:
        return code
    # Format, version, visual, byte-order and unknown typed parser failures
    # remain errors, not silent normalization to success.
    return "E_WIRE"


class PairAuditor:
    """Serialize one target's admissions. No implicit fallback or bank write on refusal."""

    def __init__(self, *, run_id: str, target_handle: Handle,
                 expected_geometry: tuple[int, int], bounds: w8u_pair.PairBounds,
                 bank: WindowStreamBank, bank_spec: StreamSpec,
                 verify_sender_completion, verify_collection, max_skew_ns: int,
                 check_cancelled=None):
        self.run_id = _run_id(run_id)
        if (not isinstance(target_handle, Handle)
                or target_handle.kind not in ("xvfb_screen", "x11_window")):
            _refuse("E_BINDING", "selected root handle must be an X window or xvfb screen")
        self.target_handle = target_handle
        if (type(expected_geometry) is not tuple or len(expected_geometry) != 2
                or any(type(side) is not int or not 0 < side <= xfb.MAX_SIDE
                       for side in expected_geometry)):
            _refuse("E_GEOMETRY", "expected_geometry requires two positive exact dimensions")
        self.expected_geometry = expected_geometry
        if not isinstance(bounds, w8u_pair.PairBounds):
            _refuse("E_LENGTH", "explicit PairBounds required")
        self.bounds = bounds
        if not isinstance(bank, WindowStreamBank) or not isinstance(bank_spec, StreamSpec):
            _refuse("E_BINDING", "real WindowStreamBank and StreamSpec required")
        if (bank_spec.width, bank_spec.height) != expected_geometry:
            _refuse("E_GEOMETRY", "bank geometry does not match bound root")
        self.bank = bank
        self.bank_spec = bank_spec
        if not callable(verify_sender_completion):
            _refuse("E_PROVENANCE", "independent grab verifier required")
        if verify_collection is not None and not callable(verify_collection):
            _refuse("E_PROVENANCE", "collection verifier must be callable or absent")
        if check_cancelled is not None and not callable(check_cancelled):
            _refuse("E_WIRE", "check_cancelled must be callable")
        self.verify_sender_completion = verify_sender_completion
        self.verify_collection = verify_collection
        self.max_skew_ns = _positive(max_skew_ns, "max_skew_ns", zero_ok=True)
        self.check_cancelled = check_cancelled
        self._lock = threading.RLock()
        self._inflight = False
        self._token = None
        self._next_seq = 1
        self._frame_count = 0
        self._ack_credits = 0
        # Retain only receipts for resident bank slots; no unbounded raw-frame log.
        self._accepted = deque(maxlen=bank_spec.slots)

    @property
    def token(self):
        """Bank token after first successful admission, else None."""
        with self._lock:
            return self._token

    @property
    def frame_count(self):
        with self._lock:
            return self._frame_count

    @property
    def ack_credits(self):
        with self._lock:
            return self._ack_credits

    @property
    def next_sequence(self):
        with self._lock:
            return self._next_seq

    def accept(self, payload, *, advertised_rgb24_sha256, sender_completion) -> AuditedFrame:
        """Perform one whole-pair admission; a refusal grants no ACK or receipt.

        ``sender_completion`` is opaque and must be resolved by both separately
        trusted capabilities. ``advertised_rgb24_sha256`` is the sender claim,
        verified against real canonical RGB24 from both XWD legs. Successful
        publication reserves the actual bank ring before adding frame credit.
        """
        with self._lock:
            if self._inflight:
                _refuse("E_BINDING", "reentrant pair admission refused")
            self._inflight = True
            try:
                return self._accept_locked(payload, advertised_rgb24_sha256,
                                           sender_completion)
            finally:
                self._inflight = False

    def _accept_locked(self, payload, claimed_sha, sender_completion):
        a, b = parse_pair_payload(payload, bounds=self.bounds)
        if claimed_sha is None:
            _refuse("E_HASH", "canonical RGB24 digest not advertised")
        grab = []
        def checked_grab(receipt):
            if receipt is not sender_completion:
                _refuse("E_PROVENANCE", "grab proof receipt identity mismatch")
            proof = self.verify_sender_completion(receipt)
            grab.append(proof)
            return proof

        def publish_after_checks(rgb_evidence):
            if len(grab) != 1 or not isinstance(grab[0], w8u_pair.VerifiedGrab):
                _refuse("E_PROVENANCE", "verified grab absent")
            if self.verify_collection is None:
                _refuse("E_PROVENANCE", "independent collection evidence absent")
            collection = self.verify_collection(sender_completion)
            _require_collection(collection, grab[0], target_handle=self.target_handle,
                                max_skew_ns=self.max_skew_ns)
            w, h = self.expected_geometry
            if (rgb_evidence.width != w or rgb_evidence.height != h
                    or rgb_evidence.stride != w * 3
                    or type(rgb_evidence.rgb24) is not bytes
                    or len(rgb_evidence.rgb24) != w * h * 3):
                _refuse("E_GEOMETRY", "canonical RGB24 stride or geometry mismatch")
            if hashlib.sha256(rgb_evidence.rgb24).hexdigest() != rgb_evidence.sha256:
                _refuse("E_HASH", "recomputed RGB24 digest mismatch")
            if rgb_evidence.mode != w8u_pair.RESIDENT_PAIR:
                _refuse("E_PROVENANCE", "unsynchronised capture cannot be admitted")
            if self._next_seq > _MAX_SEQ:
                _refuse("E_BINDING", "sequence exhausted")
            token = self._token
            opened = False
            if token is None:
                try:
                    token = self.bank.open(self.target_handle, self.bank_spec)
                except StreamError as exc:
                    raise PairAuditRefusal("E_BANK", "bank reservation refused") from exc
                opened = True
            seq = self._next_seq
            arr = np.frombuffer(rgb_evidence.rgb24, dtype=np.uint8).reshape(h, w, 3)
            try:
                self.bank.publish(token, arr, frame_seq=seq)
            except StreamError as exc:
                if opened:
                    self.bank.close(token)
                raise PairAuditRefusal("E_BANK", "bank publication refused") from exc
            identity = FrameIdentity(seq, w, h, rgb_evidence.sha256,
                                     _binding_sha256(self.run_id, token, seq, w, h,
                                                     rgb_evidence.sha256))
            admitted = AuditedFrame(identity, rgb_evidence.rgb24, w * 3,
                                    self.run_id, self.target_handle, token.epoch,
                                    w8u_pair.RESIDENT_PAIR, 1)
            self._token = token
            self._accepted.append(admitted)
            self._next_seq += 1
            self._frame_count += 1
            self._ack_credits += 1
            return admitted

        try:
            return w8u_pair.capture_resident_pair(
                lambda: w8u_pair.PairSample(a, b, sender_completion, claimed_sha),
                expected_geometry=self.expected_geometry, bounds=self.bounds,
                verify_sender_completion=checked_grab, publish=publish_after_checks,
                check_cancelled=self.check_cancelled)
        except xfb.XwdError as exc:
            raise PairAuditRefusal(_translated_xwd_code(exc.code), str(exc)) from exc

    def check_bank_frame(self, ref: FrameRef, admitted: AuditedFrame) -> FrameIdentity:
        """Validate ONE concrete bank reference, not unrelated zipped samples.

        Rejects Drop or mismatched owner, epoch, seq, geometry, digest, pixels.
        Must be called with the returned AuditedFrame, not inferred timestamps.
        """
        if not isinstance(ref, FrameRef) or not isinstance(admitted, AuditedFrame):
            _refuse("E_BINDING", "owned FrameRef and AuditedFrame required")
        identity = admitted.identity
        with self._lock:
            if not any(receipt is admitted for receipt in self._accepted):
                _refuse("E_BINDING", "frame receipt not admitted by this owner")
            current_token = self._token
        if current_token is None or current_token != StreamToken(ref.handle, ref.epoch):
            _refuse("E_BINDING", "frame token is not the active owner token")
        if (ref.handle != admitted.target_handle or ref.epoch != admitted.epoch
                or ref.frame_seq != identity.frame_seq
                or ref.width != identity.width or ref.height != identity.height
                or ref.digest != identity.rgb24_sha256
                or admitted.run_id != self.run_id
                or ref.handle != self.target_handle):
            _refuse("E_BINDING", "bank frame identity does not match owned receipt")
        if ref.rgb.tobytes(order="C") != admitted.rgb24:
            _refuse("E_TORN", "bank RGB24 differs from owned admission")
        return identity
