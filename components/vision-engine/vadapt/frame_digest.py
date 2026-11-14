"""vadapt.frame_digest - C-PIXEL canonical frame digest and committed-frame contract.

Source-neutral frame digest, layout validation, canonical RGB24 normalization,
and committed-frame lifecycle invariants (INV-J2-1 .. INV-J2-4).
"""
import hashlib
import json
import zlib
from dataclasses import dataclass, asdict
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import numpy as np

# Supported layouts and conventions
SUPPORTED_CHANNEL_ORDERS: Dict[str, int] = {
    "RGB": 3,
    "BGR": 3,
    "RGBA": 4,
    "BGRA": 4,
    "ARGB": 4,
    "ABGR": 4,
}

SUPPORTED_ORIENTATIONS = {"top_down", "bottom_up"}
SUPPORTED_COLOR_SPACES = {"srgb"}
SUPPORTED_ALPHA_CONVENTIONS = {
    "none",
    "unpremultiplied",
    "premultiplied",
    "opaque",
}

MAX_SIDE = 16384


class FrameDigestError(Exception):
    """Base exception for frame digest and committed-frame failures."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code} {message}".strip())
        self.code = code
        self.message = message


class UnsupportedLayoutError(FrameDigestError):
    """Raised when frame declaration has an unsupported or missing layout."""


class DeclarationError(FrameDigestError):
    """Raised when pixel declaration fields fail validation."""


class InvariantViolationError(FrameDigestError):
    """Raised when committed-frame invariants (INV-J2-1..4) are violated."""


class RevalidationError(InvariantViolationError):
    """Raised when seqlock/CRC revalidation fails after copying (INV-J2-3)."""


class PixelPersistenceForbiddenError(FrameDigestError):
    """Raised by write spies when pixel persistence to disk/swap is attempted."""


@dataclass(frozen=True)
class PixelDeclaration:
    """Explicit declaration of frame pixel layout and binding context.

    INV-J2-1: Declaration must be validated before reading or digesting pixels.
    Missing layout is strictly typed unsupported; never an inferred RGB default.
    """

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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alpha": self.alpha,
            "binding_id": self.binding_id,
            "channel_order": self.channel_order,
            "color_space": self.color_space,
            "dpr_ppm": self.dpr_ppm,
            "environment_id": self.environment_id,
            "height": self.height,
            "orientation": self.orientation,
            "stride_bytes": self.stride_bytes,
            "width": self.width,
            "zoom_ppm": self.zoom_ppm,
        }

    def canonical_json(self) -> str:
        d = self.to_dict()
        return json.dumps(d, sort_keys=True, separators=(",", ":"))

    @property
    def pixel_decl_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def compute_pixel_decl_sha256(decl: PixelDeclaration) -> str:
    """Deterministic hash binding the declared pixel layout."""
    return decl.pixel_decl_sha256


def validate_declaration(decl: Any) -> PixelDeclaration:
    """Validate explicit layout declaration strictly before reading buffer.

    INV-J2-1: Requires declaration before digest. Missing layout is typed
    unsupported, never an inferred RGB default.
    """
    if decl is None:
        raise UnsupportedLayoutError("E_INV_J2_1_NO_DECL", "declaration is missing; layout cannot be inferred")

    if isinstance(decl, dict):
        required_fields = {
            "channel_order",
            "stride_bytes",
            "orientation",
            "alpha",
            "color_space",
            "width",
            "height",
            "dpr_ppm",
            "zoom_ppm",
            "environment_id",
            "binding_id",
        }
        missing = required_fields - set(decl.keys())
        if missing:
            raise UnsupportedLayoutError(
                "E_LAYOUT_MISSING", f"missing mandatory declaration fields: {sorted(missing)}"
            )
        try:
            decl = PixelDeclaration(
                channel_order=decl["channel_order"],
                stride_bytes=decl["stride_bytes"],
                orientation=decl["orientation"],
                alpha=decl["alpha"],
                color_space=decl["color_space"],
                width=decl["width"],
                height=decl["height"],
                dpr_ppm=decl["dpr_ppm"],
                zoom_ppm=decl["zoom_ppm"],
                environment_id=decl["environment_id"],
                binding_id=decl["binding_id"],
            )
        except Exception as exc:
            raise UnsupportedLayoutError("E_LAYOUT_MALFORMED", f"failed to parse declaration: {exc}") from exc

    if not isinstance(decl, PixelDeclaration):
        raise UnsupportedLayoutError(
            "E_LAYOUT_TYPE", f"declaration must be PixelDeclaration or dict, got {type(decl).__name__}"
        )

    # Validate dimensions
    if type(decl.width) is not int or decl.width <= 0 or decl.width > MAX_SIDE:
        raise UnsupportedLayoutError("E_LAYOUT_GEOMETRY", f"invalid width: {decl.width}")
    if type(decl.height) is not int or decl.height <= 0 or decl.height > MAX_SIDE:
        raise UnsupportedLayoutError("E_LAYOUT_GEOMETRY", f"invalid height: {decl.height}")

    # Validate channel order
    if decl.channel_order not in SUPPORTED_CHANNEL_ORDERS:
        raise UnsupportedLayoutError("E_LAYOUT_CHANNELS", f"unsupported channel_order: {decl.channel_order}")
    bpp = SUPPORTED_CHANNEL_ORDERS[decl.channel_order]

    # Validate stride
    min_stride = decl.width * bpp
    if type(decl.stride_bytes) is not int or decl.stride_bytes < min_stride:
        raise UnsupportedLayoutError(
            "E_LAYOUT_STRIDE", f"stride_bytes {decl.stride_bytes} < minimum required {min_stride}"
        )

    # Validate orientation
    if decl.orientation not in SUPPORTED_ORIENTATIONS:
        raise UnsupportedLayoutError("E_LAYOUT_ORIENTATION", f"unsupported orientation: {decl.orientation}")

    # Validate alpha convention
    if decl.alpha not in SUPPORTED_ALPHA_CONVENTIONS:
        raise UnsupportedLayoutError("E_LAYOUT_ALPHA", f"unsupported alpha convention: {decl.alpha}")
    if bpp == 3 and decl.alpha != "none":
        raise UnsupportedLayoutError(
            "E_LAYOUT_ALPHA", f"3-channel format {decl.channel_order} requires alpha='none', got {decl.alpha}"
        )
    if bpp == 4 and decl.alpha == "none":
        raise UnsupportedLayoutError(
            "E_LAYOUT_ALPHA", f"4-channel format {decl.channel_order} cannot have alpha='none'"
        )

    # Validate color space
    if decl.color_space not in SUPPORTED_COLOR_SPACES:
        raise UnsupportedLayoutError("E_LAYOUT_COLOR_SPACE", f"unsupported color_space: {decl.color_space}")

    # Validate DPR and zoom (in parts-per-million)
    if type(decl.dpr_ppm) is not int or decl.dpr_ppm <= 0:
        raise UnsupportedLayoutError("E_LAYOUT_DPR", f"dpr_ppm must be positive int, got {decl.dpr_ppm}")
    if type(decl.zoom_ppm) is not int or decl.zoom_ppm <= 0:
        raise UnsupportedLayoutError("E_LAYOUT_ZOOM", f"zoom_ppm must be positive int, got {decl.zoom_ppm}")

    # Validate environment and binding IDs
    if not isinstance(decl.environment_id, str) or not decl.environment_id.strip():
        raise UnsupportedLayoutError("E_LAYOUT_ENVIRONMENT", "environment_id must be non-empty string")
    if not isinstance(decl.binding_id, str) or not decl.binding_id.strip():
        raise UnsupportedLayoutError("E_LAYOUT_BINDING", "binding_id must be non-empty string")

    return decl


@dataclass(frozen=True)
class FrameDigest:
    """Cryptographic frame digest with independent raw and canonical hashes."""

    raw_sha256: str
    normalized_sha256: str
    pixel_decl_sha256: str
    normalized_bytes: bytes
    declaration: PixelDeclaration
    width: int
    height: int
    stride_bytes: int


def digest_frame(
    buffer: Any,
    declaration: Any,
    *,
    revalidator: Optional[Callable[[], bool]] = None,
    write_spy: Optional[Any] = None,
) -> FrameDigest:
    """Digest a raw frame buffer under explicit declaration.

    Validates dimensions, stride, channel/alpha conventions, orientation, color space,
    and DPR/zoom strictly before reading any pixel bytes (INV-J2-1).
    Computes raw_sha256 of declared raw bytes, normalizes into separate owned RAM bytes
    (C-PIXEL standard top-down RGB24), and computes normalized_sha256.
    Revalidates sequence/CRC integrity via revalidator if provided (INV-J2-3).
    """
    # INV-J2-1: Validate declaration before reading buffer
    decl = validate_declaration(declaration)

    # Buffer type & length validation
    if buffer is None:
        raise FrameDigestError("E_BUFFER_NONE", "buffer cannot be None")

    if isinstance(buffer, np.ndarray):
        raw_mv = memoryview(buffer)
    elif isinstance(buffer, (bytes, bytearray, memoryview)):
        raw_mv = memoryview(buffer)
    else:
        try:
            raw_mv = memoryview(buffer)
        except TypeError as exc:
            raise FrameDigestError("E_BUFFER_TYPE", f"unsupported buffer type: {type(buffer).__name__}") from exc

    total_declared_bytes = decl.height * decl.stride_bytes
    if raw_mv.nbytes < total_declared_bytes:
        raise FrameDigestError(
            "E_BUFFER_SHORT", f"buffer length {raw_mv.nbytes} < declared frame size {total_declared_bytes}"
        )

    # Extract declared raw slice
    raw_slice = raw_mv[:total_declared_bytes]

    # Compute raw_sha256 of declared raw bytes
    raw_sha256 = hashlib.sha256(raw_slice).hexdigest()

    # Normalization to canonical top-down RGB24 (C-PIXEL standard) into separate owned RAM bytes
    bpp = SUPPORTED_CHANNEL_ORDERS[decl.channel_order]
    raw_arr = np.frombuffer(raw_slice, dtype=np.uint8, count=total_declared_bytes).reshape((decl.height, decl.stride_bytes))

    # Slice active row pixels (strip stride padding)
    pixels = raw_arr[:, : decl.width * bpp].reshape((decl.height, decl.width, bpp))

    # Channel reordering mapping
    if decl.channel_order == "RGB":
        r_idx, g_idx, b_idx, a_idx = 0, 1, 2, None
    elif decl.channel_order == "BGR":
        r_idx, g_idx, b_idx, a_idx = 2, 1, 0, None
    elif decl.channel_order == "RGBA":
        r_idx, g_idx, b_idx, a_idx = 0, 1, 2, 3
    elif decl.channel_order == "BGRA":
        r_idx, g_idx, b_idx, a_idx = 2, 1, 0, 3
    elif decl.channel_order == "ARGB":
        r_idx, g_idx, b_idx, a_idx = 1, 2, 3, 0
    elif decl.channel_order == "ABGR":
        r_idx, g_idx, b_idx, a_idx = 3, 2, 1, 0
    else:
        raise UnsupportedLayoutError("E_LAYOUT_CHANNELS", f"unsupported channel_order {decl.channel_order}")

    # Handle alpha normalization
    if decl.alpha == "premultiplied":
        alpha_col = pixels[:, :, a_idx].astype(np.float32)
        mask = alpha_col > 0
        rgb_f = pixels[:, :, [r_idx, g_idx, b_idx]].astype(np.float32)
        for c in range(3):
            rgb_f[:, :, c] = np.where(
                mask, np.clip(np.round(rgb_f[:, :, c] * 255.0 / alpha_col), 0, 255), 0
            )
        rgb = rgb_f.astype(np.uint8)
    else:
        rgb = pixels[:, :, [r_idx, g_idx, b_idx]]

    # Handle vertical orientation (flip bottom_up to top_down canonical row order)
    if decl.orientation == "bottom_up":
        rgb = rgb[::-1, :, :]

    # Owned, contiguous RAM bytes
    normalized_bytes = bytes(np.ascontiguousarray(rgb).tobytes())

    # Check write spies (verify no persistence to disk/swap)
    if write_spy is not None and getattr(write_spy, "attempted_write", False):
        raise PixelPersistenceForbiddenError("E_PERSISTENCE_FORBIDDEN", "frame persistence to disk or swap is forbidden")

    # INV-J2-3: sequence / CRC revalidation after copying into owned memory
    if revalidator is not None:
        try:
            valid = revalidator()
        except Exception as exc:
            raise RevalidationError("E_INV_J2_3_REVALIDATION_FAILED", f"revalidation raised: {exc}") from exc
        if not valid:
            raise RevalidationError("E_INV_J2_3_TORN", "revalidation check failed after copying (torn or reordered frame)")

    # Compute normalized_sha256 of canonical RGB24
    normalized_sha256 = hashlib.sha256(normalized_bytes).hexdigest()

    return FrameDigest(
        raw_sha256=raw_sha256,
        normalized_sha256=normalized_sha256,
        pixel_decl_sha256=decl.pixel_decl_sha256,
        normalized_bytes=normalized_bytes,
        declaration=decl,
        width=decl.width,
        height=decl.height,
        stride_bytes=decl.stride_bytes,
    )


@dataclass(frozen=True)
class CommittedFrame:
    """Frame committed with versioned metadata and lifecycle invariants."""

    digest: FrameDigest
    frame_seq: int
    committed_tick: int
    state_sha256: str
    observer_ref: str
    handle_generation: int
    epoch: int
    publication_identity: str
    window_id: Optional[str] = None
    crop_bounds: Optional[Tuple[int, int, int, int]] = None


class CommitTracker:
    """Tracks publication bindings and prevents invalid same-tick commits."""

    def __init__(self):
        self._tick_commits: Dict[int, List[CommittedFrame]] = {}

    def commit(
        self,
        digest: FrameDigest,
        *,
        frame_seq: int,
        committed_tick: int,
        state_sha256: str,
        observer_ref: str,
        handle_generation: int,
        epoch: int,
        publication_identity: str,
        render_index: Optional[int] = None,
        source_kind: str = "godot",
        window_id: Optional[str] = None,
        crop_bounds: Optional[Tuple[int, int, int, int]] = None,
        residency_observed: bool = True,
        capture_supported: bool = True,
    ) -> CommittedFrame:
        # INV-J2-4: block unsupported capture and unobserved residency
        if not capture_supported:
            raise InvariantViolationError("E_INV_J2_4_CAPTURE", "unsupported capture source blocked")
        if not residency_observed:
            raise InvariantViolationError("E_INV_J2_4_RESIDENCY", "unobserved memory residency blocked")

        # render_index is NEVER a simulation tick
        if render_index is not None and render_index == committed_tick:
            raise InvariantViolationError(
                "E_RENDER_INDEX_IS_NOT_TICK",
                f"render_index {render_index} cannot be substituted for simulation tick {committed_tick}",
            )

        # INV-J2-2: requires handle generation, epoch, geometry, and completed publication identity before comparison
        if type(handle_generation) is not int or handle_generation < 0:
            raise InvariantViolationError("E_INV_J2_2_HANDLE", f"invalid handle generation: {handle_generation}")
        if type(epoch) is not int or epoch < 0:
            raise InvariantViolationError("E_INV_J2_2_EPOCH", f"invalid epoch: {epoch}")
        if not publication_identity or not isinstance(publication_identity, str):
            raise InvariantViolationError("E_INV_J2_2_PUB_ID", "publication_identity must be non-empty string")

        # Chromium screen vs tab identity
        if source_kind == "cdp":
            if not window_id or crop_bounds is None:
                raise InvariantViolationError(
                    "E_UNTRUSTED_TAB_IDENTITY",
                    "Chromium screen requires trusted window/crop association; capturePage alone supplies no coherence",
                )

        # Same-tick distinct commits require an independent publication binding or explicit refusal
        if committed_tick in self._tick_commits:
            existing = self._tick_commits[committed_tick]
            for prior in existing:
                if prior.publication_identity == publication_identity:
                    raise InvariantViolationError(
                        "E_SAME_TICK_DUPLICATE_PUB",
                        f"duplicate publication identity {publication_identity} at tick {committed_tick}",
                    )
                if prior.digest.normalized_sha256 != digest.normalized_sha256 and not publication_identity:
                    raise InvariantViolationError(
                        "E_SAME_TICK_REFUSAL",
                        f"distinct commit at tick {committed_tick} without independent publication binding refused",
                    )

        committed = CommittedFrame(
            digest=digest,
            frame_seq=frame_seq,
            committed_tick=committed_tick,
            state_sha256=state_sha256,
            observer_ref=observer_ref,
            handle_generation=handle_generation,
            epoch=epoch,
            publication_identity=publication_identity,
            window_id=window_id,
            crop_bounds=crop_bounds,
        )

        self._tick_commits.setdefault(committed_tick, []).append(committed)
        return committed


def create_legacy_receipt(
    item: Union[FrameDigest, CommittedFrame],
    tick: Optional[int] = None,
    telemetry: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Generate legacy receipt with exactly frame_sha256, dimensions, tick, and telemetry.

    Richer metadata belongs strictly outside that receipt.
    """
    if isinstance(item, CommittedFrame):
        digest = item.digest
        frame_tick = item.committed_tick if tick is None else tick
    elif isinstance(item, FrameDigest):
        digest = item
        if tick is None:
            raise FrameDigestError("E_LEGACY_TICK_REQUIRED", "tick is required when building receipt from FrameDigest")
        frame_tick = tick
    else:
        raise FrameDigestError("E_INVALID_RECEIPT_ITEM", f"expected FrameDigest or CommittedFrame, got {type(item)}")

    return {
        "dimensions": [digest.width, digest.height],
        "frame_sha256": digest.normalized_sha256,
        "telemetry": telemetry if telemetry is not None else {},
        "tick": frame_tick,
    }


class WriteSpy:
    """Write spy that detects attempts to persist pixel frames to disk or swap."""

    def __init__(self):
        self.attempted_write = False
        self.recorded_targets: List[str] = []

    def record_attempt(self, target: str):
        self.attempted_write = True
        self.recorded_targets.append(target)
        raise PixelPersistenceForbiddenError(
            "E_PERSISTENCE_FORBIDDEN", f"write spy intercepted attempted pixel persistence to: {target}"
        )


@dataclass(frozen=True)
class T2Submission:
    """External T2 fields remain strictly tick, seq, cmd, and args."""

    tick: int
    seq: int
    cmd: str
    args: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "args": self.args,
            "cmd": self.cmd,
            "seq": self.seq,
            "tick": self.tick,
        }


def make_holdings_record(cmd: str, tick: int, seq: int, **kwargs) -> T2Submission:
    """Construct Holdings records inline using COMMANDS.md and CONTRACTS.md field names.

    - pause has empty args
    - new_game has country_id, difficulty, opening_mode, seed
    """
    if cmd == "pause":
        if kwargs:
            raise DeclarationError("E_HOLDINGS_PAUSE_ARGS", f"pause command requires empty args, got {kwargs}")
        return T2Submission(tick=tick, seq=seq, cmd="pause", args={})
    elif cmd == "new_game":
        required = {"country_id", "difficulty", "opening_mode", "seed"}
        missing = required - set(kwargs.keys())
        extra = set(kwargs.keys()) - required
        if missing or extra:
            raise DeclarationError(
                "E_HOLDINGS_NEW_GAME_FIELDS",
                f"new_game requires exact fields {sorted(required)}, missing {sorted(missing)}, extra {sorted(extra)}",
            )
        args = {
            "country_id": kwargs["country_id"],
            "difficulty": kwargs["difficulty"],
            "opening_mode": kwargs["opening_mode"],
            "seed": kwargs["seed"],
        }
        return T2Submission(tick=tick, seq=seq, cmd="new_game", args=args)
    else:
        return T2Submission(tick=tick, seq=seq, cmd=cmd, args=kwargs)


@dataclass(frozen=True)
class InjectedObservation:
    """Source-neutral injected observation fixture."""

    source_kind: str
    tree_data: Dict[str, Any]
    frame_buffer: Any
    declaration: PixelDeclaration
    window_id: Optional[str] = None
    crop_bounds: Optional[Tuple[int, int, int, int]] = None

    def __post_init__(self):
        if self.source_kind == "cdp":
            # source.kind cdp is reserved for CDP-derived DOM/AX, never a relabeled Godot tree
            if "godot_node" in self.tree_data or "scene_root" in self.tree_data or self.tree_data.get("kind") == "godot":
                raise InvariantViolationError(
                    "E_INVALID_CDP_SOURCE", "source.kind cdp cannot contain relabeled Godot tree data"
                )


class SourceNeutralBoundary:
    """Source-neutral boundary managing observations, digests, T2 submission, and outcomes.

    Admission is not economic acceptance; rejected attempts advance trusted bookkeeping
    without simulation mutation.
    """

    def __init__(self):
        self.tracker = CommitTracker()
        self.submissions: List[T2Submission] = []
        self.rejected_attempts: List[Dict[str, Any]] = []
        self.committed_frames: List[CommittedFrame] = []
        self.simulation_state_version: int = 0
        self.bookkeeping_counter: int = 0

    def submit_t2(self, submission: T2Submission, *, admit: bool = True, reason: str = "") -> bool:
        if not admit:
            # Advance trusted bookkeeping without mutating simulation state
            self.bookkeeping_counter += 1
            self.rejected_attempts.append({
                "submission": submission,
                "reason": reason,
                "bookkeeping_seq": self.bookkeeping_counter,
            })
            return False

        self.submissions.append(submission)
        self.simulation_state_version += 1
        return True

    def process_observation(
        self,
        observation: InjectedObservation,
        *,
        frame_seq: int,
        committed_tick: int,
        state_sha256: str,
        observer_ref: str,
        handle_generation: int,
        epoch: int,
        publication_identity: str,
        render_index: Optional[int] = None,
        write_spy: Optional[WriteSpy] = None,
    ) -> CommittedFrame:
        digest = digest_frame(observation.frame_buffer, observation.declaration, write_spy=write_spy)
        committed = self.tracker.commit(
            digest,
            frame_seq=frame_seq,
            committed_tick=committed_tick,
            state_sha256=state_sha256,
            observer_ref=observer_ref,
            handle_generation=handle_generation,
            epoch=epoch,
            publication_identity=publication_identity,
            render_index=render_index,
            source_kind=observation.source_kind,
            window_id=observation.window_id,
            crop_bounds=observation.crop_bounds,
        )
        self.committed_frames.append(committed)
        return committed

    def reset(self):
        self.tracker = CommitTracker()
        self.submissions.clear()
        self.rejected_attempts.clear()
        self.committed_frames.clear()
        self.simulation_state_version = 0
        self.bookkeeping_counter = 0
