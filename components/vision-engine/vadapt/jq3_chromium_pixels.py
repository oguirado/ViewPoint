"""jq3_chromium_pixels: chromium capture."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import re
import threading
import time
from typing import Callable
import numpy as np
from vadapt import window_stream_bank

from ._chromium_pixel_layout import (
    MAX_DIM, MAX_STRIDE_BYTES, MAX_FRAME_BYTES, MIN_PPM, MAX_PPM, DEFAULT_PPM, CHANNEL_ORDERS,
    THREE_CHANNEL_ORDERS, ALPHA_CHANNEL_ORDERS, PAD_CHANNEL_ORDERS, ORIENTATIONS,
    ALPHA_POLICIES, COLOR_SPACES, VALID_RESIDENCIES, CLOSED_STATES, RECEIPT_FIELDS, _ENV_ID_RE,
    _LAYOUT_FIELD_NAMES, ChromiumPixelError, _exact_int, _validate_environment_id,
    PixelLayoutDeclaration, validate_layout_declaration, NormalizedFrame, _extract_channels,
    _apply_alpha_policy, normalize_chromium_pixels, RamPixelLease,
)

@dataclass(frozen=True)
class PresentationTreeSnapshot:
    """Snapshot of the Chromium presentation/accessibility/layout tree at a specific revision."""

    revision: int
    environment_id: str
    width: int
    height: int
    dpr_ppm: int = DEFAULT_PPM
    zoom_ppm: int = DEFAULT_PPM
    nodes: tuple[dict[str, object], ...] = ()
    tree_digest: str = ""

    def __post_init__(self) -> None:
        _exact_int(self.revision, "revision", "E_TREE_REVISION", minimum=0)
        _validate_environment_id(self.environment_id, "E_TREE_ENVIRONMENT")
        _exact_int(
            self.width, "width", "E_TREE_GEOMETRY", minimum=1, maximum=MAX_DIM
        )
        _exact_int(
            self.height, "height", "E_TREE_GEOMETRY", minimum=1, maximum=MAX_DIM
        )
        _exact_int(
            self.dpr_ppm, "dpr_ppm", "E_TREE_DPR", minimum=MIN_PPM, maximum=MAX_PPM
        )
        _exact_int(
            self.zoom_ppm,
            "zoom_ppm",
            "E_TREE_ZOOM",
            minimum=MIN_PPM,
            maximum=MAX_PPM,
        )
        if not isinstance(self.nodes, tuple):
            object.__setattr__(self, "nodes", tuple(self.nodes))
        if not self.tree_digest:
            canonical = json.dumps(
                {
                    "revision": self.revision,
                    "environment_id": self.environment_id,
                    "width": self.width,
                    "height": self.height,
                    "dpr_ppm": self.dpr_ppm,
                    "zoom_ppm": self.zoom_ppm,
                    "nodes": list(self.nodes),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            object.__setattr__(
                self, "tree_digest", hashlib.sha256(canonical).hexdigest()
            )

    @property
    def scale_ppm(self) -> int:
        return compose_scale_ppm(self.dpr_ppm, self.zoom_ppm)

    def project_nodes_to_fb(self, dx: int = 0, dy: int = 0) -> tuple[dict[str, object], ...]:
        return project_tree_nodes_to_fb(self.nodes, dpr_ppm=self.dpr_ppm, zoom_ppm=self.zoom_ppm, dx=dx, dy=dy)


@dataclass(frozen=True)
class CompositorAttestation:
    """Explicit injected compositor residency and revision attestation."""

    residency: str | None
    revision_evidence: str | None
    presentation_revision: int | None
    environment_id: str = "chromium-ram-env"
    dpr_ppm: int = DEFAULT_PPM
    zoom_ppm: int = DEFAULT_PPM


@dataclass(frozen=True)
class ReadinessStatus:
    """Result of evaluating live compositor residency and revision evidence."""

    state: str
    reason: str
    residency: str | None
    presentation_revision: int | None


def evaluate_live_readiness(
    attestation: object, declaration: object | None = None
) -> ReadinessStatus:
    """Keep the live path BLOCKED when compositor residency or revision evidence is unknown."""
    if attestation is None:
        return ReadinessStatus(
            state="BLOCKED",
            reason="missing compositor attestation",
            residency=None,
            presentation_revision=None,
        )
    residency = getattr(attestation, "residency", None)
    revision_evidence = getattr(attestation, "revision_evidence", None)
    presentation_revision = getattr(attestation, "presentation_revision", None)
    environment_id = getattr(attestation, "environment_id", None)
    dpr_ppm = getattr(attestation, "dpr_ppm", None)
    zoom_ppm = getattr(attestation, "zoom_ppm", None)

    if type(residency) is not str or residency not in VALID_RESIDENCIES:
        return ReadinessStatus(
            state="BLOCKED",
            reason=f"unknown or unverified compositor residency {residency!r}",
            residency=residency if isinstance(residency, str) else None,
            presentation_revision=presentation_revision
            if type(presentation_revision) is int
            else None,
        )
    if (
        type(revision_evidence) is not str
        or not revision_evidence.strip()
        or revision_evidence.strip().lower() == "unknown"
    ):
        return ReadinessStatus(
            state="BLOCKED",
            reason="unknown or missing compositor revision evidence",
            residency=residency,
            presentation_revision=presentation_revision
            if type(presentation_revision) is int
            else None,
        )
    if type(presentation_revision) is not int or presentation_revision < 0:
        return ReadinessStatus(
            state="BLOCKED",
            reason="invalid or unknown compositor presentation_revision",
            residency=residency,
            presentation_revision=None,
        )
    if (
        type(environment_id) is not str
        or _ENV_ID_RE.fullmatch(environment_id) is None
    ):
        return ReadinessStatus(
            state="BLOCKED",
            reason="invalid or unknown compositor environment_id",
            residency=residency,
            presentation_revision=presentation_revision,
        )
    if type(dpr_ppm) is not int or not (MIN_PPM <= dpr_ppm <= MAX_PPM):
        return ReadinessStatus(
            state="BLOCKED",
            reason="invalid compositor dpr_ppm",
            residency=residency,
            presentation_revision=presentation_revision,
        )
    if type(zoom_ppm) is not int or not (MIN_PPM <= zoom_ppm <= MAX_PPM):
        return ReadinessStatus(
            state="BLOCKED",
            reason="invalid compositor zoom_ppm",
            residency=residency,
            presentation_revision=presentation_revision,
        )
    if declaration is not None:
        decl = validate_layout_declaration(declaration)
        if (
            environment_id != decl.environment_id
            or dpr_ppm != decl.dpr_ppm
            or zoom_ppm != decl.zoom_ppm
        ):
            return ReadinessStatus(
                state="BLOCKED",
                reason="compositor attestation scale/environment disagrees with declaration",
                residency=residency,
                presentation_revision=presentation_revision,
            )
    return ReadinessStatus(
        state="READY",
        reason="ok",
        residency=residency,
        presentation_revision=presentation_revision,
    )


@dataclass(frozen=True)
class CaptureReceipt:
    """Closed metadata receipt for a capture transaction; never stores pixel bytes (INV-E3-4)."""

    state: str
    handle: str
    epoch: int
    frame_seq: int
    presentation_revision: int
    tree_revision: int
    tree_digest: str
    raw_sha256: str
    normalized_sha256: str
    width: int
    height: int
    channel_order: str
    stride_bytes: int
    orientation: str
    alpha_policy: str
    color_space: str
    dpr_ppm: int
    zoom_ppm: int
    environment_id: str
    memory_released: bool
    elapsed_ns: int

    def __post_init__(self) -> None:
        if self.state not in CLOSED_STATES:
            raise ChromiumPixelError(
                "E_RECEIPT_STATE", f"invalid closed state {self.state!r}"
            )
        if self.memory_released is not True:
            raise ChromiumPixelError(
                "E_PERSISTENT_PIXELS_FORBIDDEN",
                "receipt cannot be issued before transient pixel memory is released",
            )
        for fname in RECEIPT_FIELDS:
            val = getattr(self, fname)
            if isinstance(
                val, (bytes, bytearray, memoryview, np.ndarray, RamPixelLease)
            ):
                raise ChromiumPixelError(
                    "E_PERSISTENT_PIXELS_FORBIDDEN",
                    f"receipt field {fname} must not contain raw pixel buffers",
                )

    def to_dict(self) -> dict[str, object]:
        return {k: getattr(self, k) for k in RECEIPT_FIELDS}


@dataclass(frozen=True)
class CaptureTransactionResult:
    """Completed capture transaction bundle."""

    receipt: CaptureReceipt
    frame_ref: window_stream_bank.FrameRef | None
    normalized: NormalizedFrame
    tree: PresentationTreeSnapshot


class ChromiumStreamAdapter:
    """Consume JQ2-B WindowStreamBank for a single cdp_tab handle without creating a second stream."""

    def __init__(
        self,
        bank: window_stream_bank.WindowStreamBank,
        handle: window_stream_bank.Handle,
        spec: window_stream_bank.StreamSpec,
        *,
        token: window_stream_bank.StreamToken | None = None,
    ):
        if not isinstance(bank, window_stream_bank.WindowStreamBank):
            raise ChromiumPixelError(
                "E_STREAM_BANK", "bank must be a WindowStreamBank instance"
            )
        norm_handle = window_stream_bank._handle(handle)
        if norm_handle.kind != "cdp_tab":
            raise ChromiumPixelError(
                "E_STREAM_HANDLE_KIND",
                "ChromiumStreamAdapter requires handle.kind == 'cdp_tab'",
            )
        norm_spec = window_stream_bank._spec(spec)
        self._bank = bank
        self._handle = norm_handle
        self._spec = norm_spec
        self._token: window_stream_bank.StreamToken | None = (
            window_stream_bank._token(token) if token is not None else None
        )
        if self._token is not None and self._token.handle != self._handle:
            raise ChromiumPixelError(
                "E_STREAM_TOKEN",
                "supplied token handle does not match adapter handle",
            )
        self._lock = threading.RLock()

    @property
    def bank(self) -> window_stream_bank.WindowStreamBank:
        return self._bank

    @property
    def handle(self) -> window_stream_bank.Handle:
        return self._handle

    @property
    def spec(self) -> window_stream_bank.StreamSpec:
        return self._spec

    @property
    def token(self) -> window_stream_bank.StreamToken | None:
        with self._lock:
            return self._token

    def ensure_open(self) -> window_stream_bank.StreamToken:
        """Open the stream on the shared JQ2-B WindowStreamBank once, or reuse the bound token."""
        with self._lock:
            if self._token is not None:
                return self._token
            # Delegates directly to WindowStreamBank.open -> open_many -> _staging.
            # Missing staging bytes in ring_factory raise StreamError("ring_factory returned no writable buffer").
            self._token = self._bank.open(self._handle, self._spec)
            return self._token

    def forbid_second_stream(
        self,
        other_bank: window_stream_bank.WindowStreamBank | None = None,
        other_handle: window_stream_bank.Handle | None = None,
    ) -> None:
        """Refuse any attempt to create a second JQ2-B stream or bind a second bank."""
        with self._lock:
            if other_bank is not None and other_bank is not self._bank:
                raise ChromiumPixelError(
                    "E_SECOND_STREAM_FORBIDDEN",
                    "cannot create a second JQ2-B WindowStreamBank stream for Chromium capture",
                )
            if (
                other_handle is not None
                and window_stream_bank._handle(other_handle) != self._handle
            ):
                raise ChromiumPixelError(
                    "E_SECOND_STREAM_FORBIDDEN",
                    "cannot bind a second handle on single-stream ChromiumStreamAdapter",
                )
            if self._token is not None:
                raise ChromiumPixelError(
                    "E_SECOND_STREAM_FORBIDDEN",
                    "stream is already open for this Chromium handle; reuse existing token",
                )

    def publish_frame(
        self, normalized: NormalizedFrame, *, frame_seq: int
    ) -> window_stream_bank.FrameRef:
        """Publish normalized RGB24 into the existing JQ2-B stream and return a verified FrameRef."""
        if not isinstance(normalized, NormalizedFrame):
            raise ChromiumPixelError("E_FRAME_TYPE", "expected NormalizedFrame")
        if (
            normalized.width != self._spec.width
            or normalized.height != self._spec.height
        ):
            raise ChromiumPixelError(
                "E_STREAM_GEOMETRY",
                "normalized frame geometry does not match StreamSpec",
            )
        with self._lock:
            tok = self.ensure_open()
            self._bank.publish(tok, normalized.rgb, frame_seq=frame_seq)
            ref = self._bank.read(tok)
            if isinstance(ref, window_stream_bank.Drop):
                raise ChromiumPixelError(
                    "E_STREAM_DROP",
                    f"stream read after publish returned Drop({ref.reason})",
                )
            if ref.digest != normalized.normalized_sha256:
                raise ChromiumPixelError(
                    "E_STREAM_DIGEST_MISMATCH",
                    "stream digest disagrees with normalized_sha256",
                )
            return ref

    def close(self) -> None:
        with self._lock:
            if self._token is not None:
                tok, self._token = self._token, None
                self._bank.close(tok)


class FakeChromiumProducer:
    """In-RAM fake Chromium compositor/CDP producer with presentation freeze and revision tracking."""

    def __init__(
        self,
        *,
        declaration: PixelLayoutDeclaration,
        raw_bytes: bytes | bytearray,
        tree: PresentationTreeSnapshot,
        attestation: CompositorAttestation,
        tamper_revision_on_recheck: int | None = None,
        frame_revision_override: int | None = None,
    ):
        self._lock = threading.RLock()
        self._declaration = validate_layout_declaration(declaration)
        self._raw_bytes = bytes(raw_bytes)
        self._tree = tree
        self._attestation = attestation
        self._revision = (
            attestation.presentation_revision
            if type(attestation.presentation_revision) is int
            else tree.revision
        )
        self._frame_revision_override = frame_revision_override
        self._tamper_revision_on_recheck = tamper_revision_on_recheck
        self._frozen = False
        self._freeze_count = 0
        self._unfreeze_count = 0
        self._deferred_updates: list[
            tuple[int, bytes, PixelLayoutDeclaration, PresentationTreeSnapshot]
        ] = []
        self._issued_leases: list[RamPixelLease] = []

    @property
    def frozen(self) -> bool:
        with self._lock:
            return self._frozen

    @property
    def freeze_count(self) -> int:
        with self._lock:
            return self._freeze_count

    @property
    def unfreeze_count(self) -> int:
        with self._lock:
            return self._unfreeze_count

    @property
    def issued_leases(self) -> tuple[RamPixelLease, ...]:
        with self._lock:
            return tuple(self._issued_leases)

    def attestation(self) -> CompositorAttestation:
        with self._lock:
            return self._attestation

    def freeze_presentation(self) -> int:
        with self._lock:
            self._frozen = True
            self._freeze_count += 1
            return self._revision

    def unfreeze_presentation(self) -> None:
        with self._lock:
            self._frozen = False
            self._unfreeze_count += 1
            while self._deferred_updates:
                new_rev, new_raw, new_decl, new_tree = (
                    self._deferred_updates.pop(0)
                )
                self._revision = new_rev
                self._raw_bytes = bytes(new_raw)
                self._declaration = validate_layout_declaration(new_decl)
                self._tree = new_tree
                if isinstance(self._attestation, CompositorAttestation):
                    self._attestation = CompositorAttestation(
                        residency=self._attestation.residency,
                        revision_evidence=self._attestation.revision_evidence,
                        presentation_revision=new_rev,
                        environment_id=new_decl.environment_id,
                        dpr_ppm=new_decl.dpr_ppm,
                        zoom_ppm=new_decl.zoom_ppm,
                    )

    def stage_presentation_update(
        self,
        new_revision: int,
        new_raw_bytes: bytes | bytearray,
        new_declaration: PixelLayoutDeclaration,
        new_tree: PresentationTreeSnapshot,
        *,
        bypass_freeze: bool = False,
    ) -> bool:
        """Apply a presentation update immediately if unfrozen (or bypass_freeze=True), else defer until unfreeze."""
        with self._lock:
            if self._frozen and not bypass_freeze:
                self._deferred_updates.append(
                    (
                        new_revision,
                        bytes(new_raw_bytes),
                        new_declaration,
                        new_tree,
                    )
                )
                return False
            self._revision = new_revision
            self._raw_bytes = bytes(new_raw_bytes)
            self._declaration = validate_layout_declaration(new_declaration)
            self._tree = new_tree
            if isinstance(self._attestation, CompositorAttestation):
                self._attestation = CompositorAttestation(
                    residency=self._attestation.residency,
                    revision_evidence=self._attestation.revision_evidence,
                    presentation_revision=new_revision,
                    environment_id=new_declaration.environment_id,
                    dpr_ppm=new_declaration.dpr_ppm,
                    zoom_ppm=new_declaration.zoom_ppm,
                )
            return True

    def read_ram_pixels(
        self,
    ) -> tuple[RamPixelLease, int, PixelLayoutDeclaration]:
        with self._lock:
            lease = RamPixelLease(self._raw_bytes)
            self._issued_leases.append(lease)
            frev = (
                self._revision
                if self._frame_revision_override is None
                else self._frame_revision_override
            )
            return lease, frev, self._declaration

    def read_presentation_tree(self) -> PresentationTreeSnapshot:
        with self._lock:
            return self._tree

    def current_revision(self) -> int:
        with self._lock:
            if self._tamper_revision_on_recheck is not None:
                return self._tamper_revision_on_recheck
            return self._revision


def execute_capture_transaction(
    producer: FakeChromiumProducer,
    *,
    stream_adapter: ChromiumStreamAdapter | None = None,
    frame_seq: int = 0,
    declaration_override: object | None = None,
    cancel_event: threading.Event | None = None,
    clock: Callable[[], int] = time.monotonic_ns,
    timeout_ns: int = 2_000_000_000,
    step_hook: Callable[[str], None] | None = None,
    disk_sink: object | None = None,
) -> CaptureTransactionResult:
    """Execute one atomic Chromium RAM capture transaction (INV-E3-1..INV-E3-4)."""
    if disk_sink is not None:
        raise ChromiumPixelError(
            "E_PERSISTENT_PIXELS_FORBIDDEN",
            "writing frames or clips to disk is forbidden",
        )
    _exact_int(frame_seq, "frame_seq", "E_FRAME_SEQ", minimum=0)
    _exact_int(timeout_ns, "timeout_ns", "E_TIMEOUT_CONFIG", minimum=1)
    if not callable(clock):
        raise ChromiumPixelError("E_CLOCK", "clock must be callable")
    if step_hook is not None and not callable(step_hook):
        raise ChromiumPixelError("E_STEP_HOOK", "step_hook must be callable")

    start_ns = clock()
    if type(start_ns) is not int or start_ns < 0:
        raise ChromiumPixelError(
            "E_CLOCK", "clock must return non-negative int nanoseconds"
        )

    # If caller provided an explicit declaration_override, validate it first (INV-E3-1).
    validated_override = (
        validate_layout_declaration(declaration_override)
        if declaration_override is not None
        else None
    )

    lease: RamPixelLease | None = None
    frozen = False

    def _guard(stage: str) -> int:
        if step_hook is not None:
            step_hook(stage)
        if cancel_event is not None and cancel_event.is_set():
            raise ChromiumPixelError(
                "E_CANCELLED",
                f"capture transaction cancelled at {stage}",
                state="CANCELLED",
            )
        now = clock()
        if type(now) is not int or now < start_ns:
            raise ChromiumPixelError(
                "E_CLOCK", "clock must be monotonic non-negative int"
            )
        if now - start_ns > timeout_ns:
            raise ChromiumPixelError(
                "E_TIMEOUT",
                f"capture transaction timed out at {stage}",
                state="TIMED_OUT",
            )
        return now

    try:
        _guard("before_attestation")
        att = producer.attestation()
        readiness = evaluate_live_readiness(att, validated_override)
        if readiness.state != "READY":
            raise ChromiumPixelError(
                "E_LIVE_BLOCKED", readiness.reason, state="BLOCKED"
            )

        _guard("before_freeze")
        rev_frozen = producer.freeze_presentation()
        frozen = True
        if type(rev_frozen) is not int or rev_frozen < 0:
            raise ChromiumPixelError(
                "E_LIVE_BLOCKED",
                "freeze_presentation returned invalid revision",
                state="BLOCKED",
            )
        if rev_frozen != att.presentation_revision:
            raise ChromiumPixelError(
                "E_REVISION_MISMATCH",
                f"attestation revision {att.presentation_revision} != frozen revision {rev_frozen}",
            )

        _guard("after_freeze")
        lease, frame_rev, prod_decl = producer.read_ram_pixels()
        if not isinstance(lease, RamPixelLease):
            raise ChromiumPixelError(
                "E_RAW_BUFFER_TYPE", "producer must return a RamPixelLease"
            )

        # INV-E3-1: Validate layout declaration before hashing or reading pixels.
        decl = (
            validated_override
            if validated_override is not None
            else validate_layout_declaration(prod_decl)
        )
        post_decl_readiness = evaluate_live_readiness(att, decl)
        if post_decl_readiness.state != "READY":
            raise ChromiumPixelError(
                "E_LIVE_BLOCKED", post_decl_readiness.reason, state="BLOCKED"
            )

        _guard("after_pixels")
        tree = producer.read_presentation_tree()
        if not isinstance(tree, PresentationTreeSnapshot):
            raise ChromiumPixelError(
                "E_TREE_TYPE", "producer must return PresentationTreeSnapshot"
            )

        _guard("after_tree")
        rev_after = producer.current_revision()

        # INV-E3-3: Recheck frame, tree, and presentation revision association.
        if not (
            type(frame_rev) is int
            and type(rev_after) is int
            and rev_frozen == frame_rev == tree.revision == rev_after
        ):
            raise ChromiumPixelError(
                "E_REVISION_MISMATCH",
                f"revision mismatch: frozen={rev_frozen} frame={frame_rev} tree={tree.revision} after={rev_after}",
            )
        if (
            tree.width != decl.width
            or tree.height != decl.height
            or tree.environment_id != decl.environment_id
            or tree.dpr_ppm != decl.dpr_ppm
            or tree.zoom_ppm != decl.zoom_ppm
        ):
            raise ChromiumPixelError(
                "E_CORRELATION_MISMATCH",
                "presentation tree geometry/scale/environment disagrees with pixel declaration",
            )

        # INV-E3-1 & INV-E3-2: Normalize pixels and compute separated digests.
        normalized = normalize_chromium_pixels(lease, decl)

        _guard("before_publish")
        frame_ref: window_stream_bank.FrameRef | None = None
        handle_id = "cdp_tab:unbound:0"
        epoch = 0
        if stream_adapter is not None:
            frame_ref = stream_adapter.publish_frame(
                normalized, frame_seq=frame_seq
            )
            handle_id = f"{frame_ref.handle.kind}:{frame_ref.handle.canonical_id}:{frame_ref.handle.generation}"
            epoch = frame_ref.epoch

        # INV-E3-4: Release transient pixel memory before issuing receipt.
        lease.release()
        producer.unfreeze_presentation()
        frozen = False
        end_ns = _guard("after_publish")

        receipt = CaptureReceipt(
            state="COMMITTED",
            handle=handle_id,
            epoch=epoch,
            frame_seq=frame_seq,
            presentation_revision=rev_frozen,
            tree_revision=tree.revision,
            tree_digest=tree.tree_digest,
            raw_sha256=normalized.raw_sha256,
            normalized_sha256=normalized.normalized_sha256,
            width=decl.width,
            height=decl.height,
            channel_order=decl.channel_order,
            stride_bytes=decl.stride_bytes,
            orientation=decl.orientation,
            alpha_policy=decl.alpha_policy,
            color_space=decl.color_space,
            dpr_ppm=decl.dpr_ppm,
            zoom_ppm=decl.zoom_ppm,
            environment_id=decl.environment_id,
            memory_released=lease.released,
            elapsed_ns=end_ns - start_ns,
        )
        return CaptureTransactionResult(
            receipt=receipt,
            frame_ref=frame_ref,
            normalized=normalized,
            tree=tree,
        )
    finally:
        if lease is not None and not lease.released:
            lease.release()
        if frozen:
            try:
                producer.unfreeze_presentation()
            except Exception:
                pass




HALF_PPM = 500_000


def _projection_int(value, name="coordinate"):
    if type(value) is not int:
        raise ChromiumPixelError("E_TREE_GEOMETRY", name + " must be an exact int")
    return value


def compose_scale_ppm(dpr_ppm=DEFAULT_PPM, zoom_ppm=DEFAULT_PPM):
    dpr = _exact_int(dpr_ppm, "dpr_ppm", "E_TREE_DPR", minimum=MIN_PPM, maximum=MAX_PPM)
    zoom = _exact_int(zoom_ppm, "zoom_ppm", "E_TREE_ZOOM", minimum=MIN_PPM, maximum=MAX_PPM)
    return _exact_int((dpr*zoom+HALF_PPM)//DEFAULT_PPM, "scale_ppm", "E_TREE_SCALE", minimum=MIN_PPM, maximum=MAX_PPM)


def project_css_coord_to_fb(css_coord, scale_ppm=DEFAULT_PPM, d=0):
    scale = _exact_int(scale_ppm, "scale_ppm", "E_TREE_SCALE", minimum=MIN_PPM, maximum=MAX_PPM)
    return (_projection_int(css_coord)*scale+HALF_PPM)//DEFAULT_PPM + _projection_int(d)


def css_box_to_closed_fb(bbox_css, scale_ppm=DEFAULT_PPM, dx=0, dy=0):
    if not isinstance(bbox_css, (list, tuple)) or len(bbox_css) != 4:
        raise ChromiumPixelError("E_TREE_GEOMETRY", "four coordinates required")
    x, y, w, h = map(_projection_int, bbox_css)
    project_css_coord_to_fb(0, scale_ppm, dx)
    project_css_coord_to_fb(0, scale_ppm, dy)
    if w <= 0 or h <= 0:
        return None
    x0, y0 = project_css_coord_to_fb(x, scale_ppm, dx), project_css_coord_to_fb(y, scale_ppm, dy)
    xe, ye = project_css_coord_to_fb(x+w, scale_ppm, dx), project_css_coord_to_fb(y+h, scale_ppm, dy)
    return (x0, y0, xe-1, ye-1) if xe > x0 and ye > y0 else None


def project_tree_nodes_to_fb(nodes, *, dpr_ppm=DEFAULT_PPM, zoom_ppm=DEFAULT_PPM, dx=0, dy=0):
    scale = compose_scale_ppm(dpr_ppm, zoom_ppm)
    project_css_coord_to_fb(0, scale, dx)
    project_css_coord_to_fb(0, scale, dy)
    out = []
    for node in nodes:
        rec = dict(node)
        box = rec.get("bbox_px")
        if box is None and rec.get("box") is not None:
            x0, y0, x1, y1 = map(_projection_int, rec["box"])
            box = (x0, y0, x1-x0+1, y1-y0+1)
        if box is not None:
            closed = css_box_to_closed_fb(box, scale, dx, dy)
            rec["box"] = list(closed) if closed is not None else None
        out.append(rec)
    return tuple(out)

from ._chromium_parity import (
    ParityReport, verify_ram_pair_parity, build_sentinel_rgb, encode_raw_chromium_buffer,
    BehavioralCaseReport, run_behavioral_case_matrix,
)
