"""Source-neutral, fail-closed binding of one completed publication to owned RAM pixels.

No X11, Godot or CDP adapter is imported. All witnesses are supplied by a
trusted caller; this module cannot attest an OS's RAM residency or fences.
"""

from dataclasses import dataclass
import hashlib
import json
import re
import zlib


MAX_FRAME_BYTES = 256 * 1024 * 1024
MAX_SIDE = 16384
METADATA_VERSION = "committed-frame-v1"
_CHANNELS = {"RGB": ("R", "G", "B"), "BGR": ("B", "G", "R"),
             "RGBA": ("R", "G", "B", "A"), "BGRA": ("B", "G", "R", "A"),
             "ARGB": ("A", "R", "G", "B"), "ABGR": ("A", "B", "G", "R")}
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class CommittedFrameError(ValueError):
    """Stable, fail-closed error with a machine-readable ``code``."""

    def __init__(self, code, detail=""):
        self.code = code
        super().__init__(code + (": " + detail if detail else ""))


def _need(test, code, detail=""):
    if not test:
        raise CommittedFrameError(code, detail)


def _integer(value, field, *, minimum=0, maximum=None):
    _need(type(value) is int and value >= minimum and
          (maximum is None or value <= maximum), "E_IDENTITY", field)
    return value


def _text(value, field):
    _need(type(value) is str and 0 < len(value) <= 256 and
          value.strip() == value and "\x00" not in value, "E_IDENTITY", field)
    return value


def _digest(value, field):
    _need(type(value) is str and _HEX64.fullmatch(value) is not None,
          "E_IDENTITY", field)
    return value


def _field(mapping, name, code="E_IDENTITY"):
    try:
        return mapping[name]
    except (KeyError, TypeError) as exc:
        raise CommittedFrameError(code, "missing " + name) from exc


def _mapping(value, code="E_IDENTITY"):
    _need(hasattr(value, "__getitem__") and not isinstance(value, (str, bytes)), code)
    return value


@dataclass(frozen=True)
class PixelDeclaration:
    channel_order: str
    stride_bytes: int
    orientation: str
    alpha: str
    color_space: str
    width: int
    height: int
    dpr_ppm: int
    zoom_ppm: int
    environment_id: str
    binding_id: str

    @classmethod
    def validated(cls, value, *, max_frame_bytes=MAX_FRAME_BYTES):
        """Validate ALL C-PIXEL fields before any pixels are read or hashed."""
        if isinstance(value, cls):
            value = vars(value)
        value = _mapping(value, "E_PIXEL_DECL")
        names = cls.__dataclass_fields__
        try:
            fields = {n: value[n] for n in names}
        except (KeyError, TypeError) as exc:
            raise CommittedFrameError("E_PIXEL_DECL", "incomplete declaration") from exc
        order = fields["channel_order"]
        _need(type(order) is str and order in _CHANNELS, "E_PIXEL_DECL", "channel_order")
        _need(fields["orientation"] in ("top-down", "bottom-up") and
              type(fields["orientation"]) is str, "E_PIXEL_DECL", "orientation")
        _need(fields["alpha"] in ("none", "opaque", "straight") and
              type(fields["alpha"]) is str, "E_PIXEL_UNSUPPORTED", "alpha")
        _need(type(fields["color_space"]) is str and fields["color_space"] == "sRGB",
              "E_PIXEL_UNSUPPORTED", "color_space")
        has_alpha = "A" in _CHANNELS[order]
        _need((has_alpha and fields["alpha"] in ("opaque", "straight")) or
              (not has_alpha and fields["alpha"] == "none"), "E_PIXEL_DECL", "alpha/order")
        for n in ("width", "height"):
            _need(type(fields[n]) is int and 0 < fields[n] <= MAX_SIDE, "E_PIXEL_DECL", n)
        for n in ("dpr_ppm", "zoom_ppm"):
            _need(type(fields[n]) is int and 0 < fields[n] <= 1_000_000_000,
                  "E_PIXEL_DECL", n)
        for n in ("environment_id", "binding_id"):
            try:
                _text(fields[n], n)
            except CommittedFrameError as exc:
                raise CommittedFrameError("E_PIXEL_DECL", n) from exc
        stride = fields["stride_bytes"]
        _need(type(stride) is int and stride >= fields["width"] * len(_CHANNELS[order]),
              "E_PIXEL_DECL", "stride_bytes")
        _need(type(max_frame_bytes) is int and max_frame_bytes > 0 and
              stride * fields["height"] <= max_frame_bytes,
              "E_PIXEL_LIMIT", "declared frame exceeds allocation bound")
        return cls(**fields)

    @property
    def sha256(self):
        canonical = json.dumps(vars(self), sort_keys=True, separators=(",", ":"),
                               ensure_ascii=True).encode("ascii")
        return hashlib.sha256(canonical).hexdigest()

    @property
    def raw_size(self):
        return self.height * self.stride_bytes


@dataclass(frozen=True)
class BoundaryIdentity:
    """Trusted request identity; presentation and committed tick are distinct."""

    source_kind: str
    handle_id: str
    handle_generation: int
    epoch: int
    width: int
    height: int
    environment_id: str
    binding_id: str
    publication_id: str
    presentation_revision: int
    frame_seq: int
    committed_tick: int
    state_sha256: str
    observer_ref: str
    window_id: str = ""
    crop: tuple = ()

    @classmethod
    def validated(cls, value):
        if isinstance(value, cls):
            value = vars(value)
        value = _mapping(value)
        fields = {n: _field(value, n) for n in cls.__dataclass_fields__ if
                  n not in ("window_id", "crop")}
        fields["window_id"] = value.get("window_id", "")
        fields["crop"] = value.get("crop", ())
        _need(fields["source_kind"] in ("godot", "cdp", "xvfb-xwd") and
              type(fields["source_kind"]) is str, "E_SOURCE_UNSUPPORTED")
        for n in ("handle_id", "environment_id", "binding_id", "publication_id", "observer_ref"):
            _text(fields[n], n)
        for n in ("handle_generation", "epoch", "presentation_revision", "frame_seq", "committed_tick"):
            _integer(fields[n], n)
        for n in ("width", "height"):
            _integer(fields[n], n, minimum=1, maximum=MAX_SIDE)
        _digest(fields["state_sha256"], "state_sha256")
        if fields["source_kind"] == "cdp":
            _text(fields["window_id"], "window_id")
            crop = fields["crop"]
            _need(type(crop) in (tuple, list) and len(crop) == 4 and
                  all(type(x) is int for x in crop) and
                  crop[0] >= 0 and crop[1] >= 0 and
                  crop[2] == fields["width"] and crop[3] == fields["height"],
                  "E_UNTRUSTED_CROP", "CDP window/crop binding required")
            fields["crop"] = tuple(crop)
        else:
            _need(fields["window_id"] in ("", None) and fields["crop"] in ((), [], None),
                  "E_IDENTITY", "window/crop only for trusted CDP")
            fields["window_id"], fields["crop"] = "", ()
        return cls(**fields)


@dataclass(frozen=True)
class CommittedPublication:
    identity: BoundaryIdentity
    pixel_decl_sha256: str
    raw_sha256: str
    normalized_sha256: str
    complete: bool
    residency_verified: bool

    @classmethod
    def validated(cls, value):
        value = _mapping(value, "E_PUBLICATION_UNAVAILABLE")
        ident = BoundaryIdentity.validated(_field(value, "identity", "E_PUBLICATION_UNAVAILABLE"))
        hashes = {n: _digest(_field(value, n, "E_PUBLICATION_UNAVAILABLE"), n)
                  for n in ("pixel_decl_sha256", "raw_sha256", "normalized_sha256")}
        complete = _field(value, "complete", "E_PUBLICATION_UNAVAILABLE")
        resident = _field(value, "residency_verified", "E_PUBLICATION_UNAVAILABLE")
        _need(complete is True, "E_PUBLICATION_INCOMPLETE")
        _need(resident is True, "E_RESIDENCY_UNOBSERVED")
        return cls(ident, **hashes, complete=True, residency_verified=True)


@dataclass(frozen=True)
class CaptureObservation:
    """Injected lease for raw bytes; revalidate must inspect source AFTER copy.

    revalidate() returns (sequence, crc32); release() must drop any borrowed view.
    A source adapter must supply the real seqlock and CRC observations, not a
    prediction based on the already copied bytes.
    """

    buffer: object
    identity: object
    slot_sequence: int
    slot_crc32: int
    revalidate: object
    release: object
    provenance: str
    coherent: bool
    residency_verified: bool


@dataclass(frozen=True)
class BoundFrameMetadata:
    identity: BoundaryIdentity
    pixel_decl_sha256: str
    raw_sha256: str
    normalized_sha256: str
    slot_sequence: int
    slot_crc32: int

    @property
    def schema_version(self):
        return METADATA_VERSION

    @property
    def frame_seq(self):
        return self.identity.frame_seq

    @property
    def committed_tick(self):
        return self.identity.committed_tick

    @property
    def state_sha256(self):
        return self.identity.state_sha256

    @property
    def observer_ref(self):
        return self.identity.observer_ref


@dataclass(frozen=True)
class BoundFrame:
    """Immutable metadata and privately owned immutable pixel buffers (RAM only)."""

    pixel_decl: PixelDeclaration
    metadata: BoundFrameMetadata
    raw_bytes: bytes
    normalized_bytes: bytes


def _own_bytes(value, size):
    try:
        view = memoryview(value)
        _need(view.nbytes == size, "E_PIXEL_SIZE", "buffer does not match declaration")
        _need(view.itemsize == 1 and view.format in ("B", "b", "c") and
              view.c_contiguous, "E_PIXEL_SOURCE", "expected contiguous unsigned byte buffer")
        # A mutable source can be re-used or overwritten immediately after this.
        # bytes(bytearray(...)) forces ownership even if source is already bytes.
        raw = bytes(bytearray(view.tobytes()))
    except (TypeError, BufferError, ValueError) as exc:
        if isinstance(exc, CommittedFrameError):
            raise
        raise CommittedFrameError("E_PIXEL_SOURCE", "expected buffer protocol") from exc
    _need(len(raw) == size, "E_PIXEL_SIZE")
    return raw


def _normalize(raw, decl):
    channels = _CHANNELS[decl.channel_order]
    n = len(channels)
    offsets = tuple(channels.index(k) for k in ("R", "G", "B"))
    want_alpha = decl.alpha == "straight"
    out = bytearray(decl.width * decl.height * (4 if want_alpha else 3))
    cursor = 0
    for y in range(decl.height):
        row = y if decl.orientation == "top-down" else decl.height - 1 - y
        start = row * decl.stride_bytes
        for x in range(decl.width):
            p = start + x * n
            out[cursor:cursor + 3] = bytes(raw[p + j] for j in offsets)
            cursor += 3
            if "A" in channels:
                a = raw[p + channels.index("A")]
                _need(a == 255 or decl.alpha == "straight", "E_PIXEL_ALPHA", "nonopaque pixel")
                if want_alpha:
                    out[cursor] = a
                    cursor += 1
    return bytes(out)


def _witness(observation):
    _need(callable(observation.revalidate), "E_CAPTURE_UNSUPPORTED", "no source revalidation")
    try:
        observed = observation.revalidate()
    except Exception as exc:
        raise CommittedFrameError("E_CAPTURE_UNAVAILABLE", "revalidation failed") from exc
    _need(type(observed) is tuple and len(observed) == 2 and
          all(type(v) is int for v in observed), "E_CAPTURE_UNSUPPORTED", "bad witness")
    return observed


def _load_publication(lookup_publication):
    try:
        return CommittedPublication.validated(lookup_publication())
    except CommittedFrameError:
        raise
    except Exception as exc:
        raise CommittedFrameError("E_PUBLICATION_UNAVAILABLE") from exc


def bind_committed_frame(*, binding, pixel_decl, capture, lookup_publication,
                         max_frame_bytes=MAX_FRAME_BYTES):
    """Bind pixels to one precisely identified *completed* committed publication.

    ``binding`` and ``lookup_publication`` are trusted, independent context.
    ``capture`` returns CaptureObservation containing borrowed raw bytes and a
    post-copy witness. This function never writes any pixels or calls adapters.
    All input identities are copied into frozen, scalar-only records. Callers
    must own and release the external frame lease; this function always invokes
    its release callback after capture returns, even on validation failure.
    """
    _need(type(max_frame_bytes) is int and max_frame_bytes > 0, "E_PIXEL_LIMIT")
    _need(callable(capture) and callable(lookup_publication), "E_CAPTURE_UNSUPPORTED")
    decl = PixelDeclaration.validated(pixel_decl, max_frame_bytes=max_frame_bytes)
    requested = BoundaryIdentity.validated(binding)
    _need((decl.width, decl.height, decl.environment_id, decl.binding_id) ==
          (requested.width, requested.height, requested.environment_id, requested.binding_id),
          "E_PIXEL_BINDING", "declaration/identity mismatch")
    before = _load_publication(lookup_publication)
    _need(before.identity == requested, "E_PUBLICATION_IDENTITY", "requested vs published")
    _need(before.pixel_decl_sha256 == decl.sha256, "E_PIXEL_BINDING", "layout changed")
    try:
        obs = capture()
    except Exception as exc:
        raise CommittedFrameError("E_CAPTURE_UNAVAILABLE") from exc
    _need(isinstance(obs, CaptureObservation), "E_CAPTURE_UNSUPPORTED", "CaptureObservation required")
    try:
        _need(callable(obs.release), "E_CAPTURE_UNSUPPORTED", "release required")
        captured_identity = BoundaryIdentity.validated(obs.identity)
        _need(captured_identity == requested, "E_CAPTURE_IDENTITY", "source is not requested commit")
        _need(obs.provenance == requested.source_kind, "E_SOURCE_PROVENANCE",
              "a Godot tree is not a CDP-derived observation")
        _need(obs.coherent is True, "E_CAPTURE_UNSUPPORTED", "capturePage alone is not coherent")
        _need(obs.residency_verified is True, "E_RESIDENCY_UNOBSERVED")
        seq = obs.slot_sequence
        crc = obs.slot_crc32
        _need(type(seq) is int and 0 <= seq < 1 << 64 and seq % 2 == 0 and
              type(crc) is int and 0 <= crc <= 0xffffffff,
              "E_CAPTURE_TORN", "invalid sequence/CRC")
        raw = _own_bytes(obs.buffer, decl.raw_size)
        _need(zlib.crc32(raw) == crc, "E_CAPTURE_TORN", "copied data CRC mismatch")
        _need(_witness(obs) == (seq, crc), "E_CAPTURE_TORN", "source changed during copy")
        after = _load_publication(lookup_publication)
        _need(after == before, "E_PUBLICATION_CHANGED", "epoch/identity/revision changed")
        _need(_witness(obs) == (seq, crc), "E_CAPTURE_TORN", "source changed after lookup")
        normalized = _normalize(raw, decl)
        raw_digest = hashlib.sha256(raw).hexdigest()
        norm_digest = hashlib.sha256(normalized).hexdigest()
        _need((raw_digest, norm_digest) == (before.raw_sha256, before.normalized_sha256),
              "E_FRAME_DIGEST", "pixels do not match committed digest")
        _need(_load_publication(lookup_publication) == before, "E_PUBLICATION_CHANGED",
              "publication changed during digest")
        _need(_witness(obs) == (seq, crc), "E_CAPTURE_TORN", "source changed before return")
        return BoundFrame(decl, BoundFrameMetadata(requested, decl.sha256,
                          raw_digest, norm_digest, seq, crc), raw, normalized)
    finally:
        if callable(obs.release):
            obs.release()
