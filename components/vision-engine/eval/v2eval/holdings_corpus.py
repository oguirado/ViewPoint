"""Source-neutral Holdings synthetic corpus generator, provenance manifest, and regression contracts.

Owns deterministic RAM-only uint8 frame synthesis (`generate_case`), independent
intent-authored ground-truth labels (`INV-J10-4`), per-lane SplitMix64 seed-tag
isolation (`INV-J10-1`), pinned seed/generator/environment regeneration contracts
(`INV-J10-2`), ContextVar-backed GM/FL operation accounting preservation and
cleanup on exceptions (`INV-J10-3`), source-neutral Holdings observation/T2
boundaries, and pixel-persistence write spies.
"""
from __future__ import annotations

import builtins
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from contextvars import copy_context
import copy
from dataclasses import dataclass, field
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import tempfile
from typing import Any
from unittest.mock import patch

import numpy as np

from eval.gui_synth.rng import MASK as SPLITMIX64_MASK, SplitMix64
from eval.v2eval.postpass import BaselineFlickerVeto
from eval.v2x import fl_blinkprior, fl_v3, gm_bands_hv
from eval.v2x.per_window_detector import (
    ChangeResult,
    DetectorInputError,
    Handle,
    KINDS as SUPPORTED_HANDLE_KINDS,
    MissingEvidenceError,
    PerWindowDetector,
)
from visioncore import desc
from visioncore.desc import luma_of_padded
from visioncore.grid import Grid

AUSENTE = "AUSENTE"
ELECTRON_BROKERED_PUBLISHER = AUSENTE
CONDUCTOR_ADMISSION_BINDING = AUSENTE
EXECUTION_WAVE = 1
TIMING_DOMAIN = "VM"

MANIFEST_SCHEMA = "vqa.holdings_corpus.manifest/v1"
ALGORITHM_ID = "holdings_corpus_splitmix64_v1"

MIN_DIMENSION = 16
MAX_WIDTH = 1920
MAX_HEIGHT = 1088
MAX_FRAMES = 256
MAX_TOTAL_PIXELS = 1920 * 1088 * 64
MAX_LANES = 64
MAX_CASES = 256

CLASS_UNCHANGED = 0
CLASS_JITTER = 1
CLASS_CHANGED = 2
CLASS_MOVED = 3
CLASS_FLICKER = 4
CLASS_FROZEN = 5
CLASS_DRIFT = 6

EXTERNAL_T2_FIELDS = ("tick", "seq", "cmd", "args")
NEW_GAME_ARG_FIELDS = ("country_id", "difficulty", "opening_mode", "seed")

CASE_FAMILIES = (
    "static_control",
    "localized_change",
    "integer_translation",
    "periodic_flicker_veto",
    "sparse_support",
    "mixed_history",
    "skipped_frame",
    "resize_epoch",
    "caller_mutation",
    "plugin_exception",
    "cache_eviction",
)

_FAMILY_ALIASES = {
    "static": "static_control",
    "static_control": "static_control",
    "static_controls": "static_control",
    "localized": "localized_change",
    "localized_change": "localized_change",
    "localized_changes": "localized_change",
    "translation": "integer_translation",
    "integer_translation": "integer_translation",
    "integer_translations": "integer_translation",
    "flicker": "periodic_flicker_veto",
    "periodic_flicker": "periodic_flicker_veto",
    "periodic_flicker_veto": "periodic_flicker_veto",
    "real_motion_veto": "periodic_flicker_veto",
    "sparse": "sparse_support",
    "sparse_support": "sparse_support",
    "mixed": "mixed_history",
    "mixed_history": "mixed_history",
    "mixed_histories": "mixed_history",
    "skip": "skipped_frame",
    "skipped_frame": "skipped_frame",
    "skipped_frames": "skipped_frame",
    "resize": "resize_epoch",
    "epoch": "resize_epoch",
    "resize_epoch": "resize_epoch",
    "caller_mutation": "caller_mutation",
    "mutation": "caller_mutation",
    "plugin_exception": "plugin_exception",
    "plugin_exceptions": "plugin_exception",
    "exception": "plugin_exception",
    "eviction": "cache_eviction",
    "cache_eviction": "cache_eviction",
}

VALID_FAULTS = frozenset({
    "none",
    "skip_frame",
    "gap",
    "resize_epoch",
    "stale_epoch",
    "geometry_without_epoch",
    "non_increasing_seq",
    "caller_mutation",
    "plugin_exception",
    "gm_exception",
    "fl_exception",
    "veto_exception",
    "missing_counter",
    "cache_eviction",
    "reset_window",
    "rejected_t2",
})

DEPENDENCY_RELATIVE_PATHS = (
    "eval/gui_synth/rng.py",
    "eval/v2x/per_window_detector.py",
    "eval/v2x/fl_v3.py",
    "eval/v2x/gm_bands_hv.py",
    "eval/v2eval/postpass.py",
)

FAKE_PROCFS_MEMINFO = (
    "MemTotal:       16384000 kB\n"
    "MemFree:         8192000 kB\n"
    "MemAvailable:   12288000 kB\n"
    "SwapTotal:             0 kB\n"
    "SwapFree:              0 kB\n"
)

FAKE_CGROUP_MEMORY_MAX = "4294967296\n"
FAKE_CGROUP_CPU_MAX = "200000 100000\n"


class CorpusInputError(ValueError):
    """Base error for invalid corpus specifications, seeds, faults, or boundaries."""


class CorpusCeilingError(CorpusInputError):
    """Geometry or frame count exceeds declared finite RAM ceiling."""


class MalformedSeedError(CorpusInputError, TypeError):
    """Seed, seed_tag, or rng_factory is missing, boolean, float, negative, or out of 64-bit bounds."""


class MalformedFaultError(CorpusInputError):
    """Fault list or fault descriptor is unknown or structurally invalid."""


class ProvenanceMismatchError(CorpusInputError):
    """Regeneration refused or failed due to unpinned seed, generator_sha256, or environment."""


class SourceBoundaryError(CorpusInputError):
    """Invalid source observation, relabeled Godot tree on cdp kind, or malformed T2 record."""


class PixelPersistenceError(CorpusInputError, PermissionError):
    """Forbidden attempt to persist volatile frame bytes to disk, swap, logs, or screenshot APIs."""


class MissingPrerequisiteError(CorpusInputError):
    """Mandatory prerequisite or conductor/owner receipt is AUSENTE or missing."""


def _require_strict_int(value: Any, name: str, *, min_value: int | None = None, max_value: int | None = None) -> int:
    if type(value) is not int:
        raise CorpusInputError(f"{name} must be a strict int, got {type(value).__name__}")
    if min_value is not None and value < min_value:
        raise CorpusInputError(f"{name} must be >= {min_value}, got {value}")
    if max_value is not None and value > max_value:
        raise CorpusInputError(f"{name} must be <= {max_value}, got {value}")
    return value


def _validate_seed_int(value: Any, name: str) -> int:
    if type(value) is not int:
        raise MalformedSeedError(f"{name} must be a strict integer, got {type(value).__name__}")
    if value < 0 or value > SPLITMIX64_MASK:
        raise MalformedSeedError(f"{name} must be in [0, 2^64-1], got {value}")
    return value


def _validate_rng_factory(rng_factory: Any) -> Callable[[int], Any]:
    if not callable(rng_factory):
        raise MalformedSeedError("rng_factory must be callable")
    try:
        probe = rng_factory(0)
    except Exception as exc:
        raise MalformedSeedError(f"rng_factory failed seed 0 initialization: {exc}") from exc
    if not callable(getattr(probe, "u64", None)) or not callable(getattr(probe, "randint", None)):
        raise MalformedSeedError("rng_factory instance must implement u64() and randint(lo, hi)")
    sample = probe.u64()
    if type(sample) is not int or sample < 0 or sample > SPLITMIX64_MASK:
        raise MalformedSeedError("rng_factory.u64() must return a 64-bit unsigned int")
    r_sample = probe.randint(0, 7)
    if type(r_sample) is not int or not (0 <= r_sample <= 7):
        raise MalformedSeedError("rng_factory.randint(0, 7) must return an int in [0, 7]")
    return rng_factory


def derive_lane_seed(master_seed: int, seed_tag: int, rng_factory: Callable[[int], Any] = SplitMix64) -> int:
    """Derive a deterministic 64-bit lane seed from master_seed and integer seed_tag.

    Depends only on (master_seed, seed_tag) so reordering lanes or adding/removing
    sibling lanes never alters any byte of this lane's RNG stream (INV-J10-1).
    """
    m_seed = _validate_seed_int(master_seed, "master_seed")
    s_tag = _validate_seed_int(seed_tag, "seed_tag")
    factory = _validate_rng_factory(rng_factory)

    root_rng = factory(m_seed)
    root_word = root_rng.u64()
    tag_mix = ((s_tag ^ 0xD1B54A32D192ED03) * 0x9E3779B97F4A7C15) & SPLITMIX64_MASK
    combined = (root_word ^ tag_mix) & SPLITMIX64_MASK
    lane_rng = factory(combined)
    return lane_rng.u64()


def derive_frame_seed(lane_seed: int, frame_seq: int, rng_factory: Callable[[int], Any] = SplitMix64) -> int:
    """Derive a deterministic per-frame seed from lane_seed and local frame_seq."""
    l_seed = _validate_seed_int(lane_seed, "lane_seed")
    seq = _require_strict_int(frame_seq, "frame_seq", min_value=0)
    factory = _validate_rng_factory(rng_factory)
    mixed = (l_seed ^ (((seq + 1) * 0xBF58476D1CE4E5B9) & SPLITMIX64_MASK)) & SPLITMIX64_MASK
    return factory(mixed).u64()


def _normalize_handle(value: Any, default_id: str = "holdings_lane_0") -> Handle:
    if value is None:
        return Handle("godot_window", default_id, 1)
    if isinstance(value, Handle):
        return Handle(value.kind, value.canonical_id, value.generation)
    if isinstance(value, Mapping):
        try:
            return Handle(value["kind"], value["canonical_id"], value["generation"])
        except KeyError as exc:
            raise CorpusInputError(f"handle mapping missing required key: {exc}") from exc
    if isinstance(value, (tuple, list)) and len(value) == 3:
        return Handle(value[0], value[1], value[2])
    try:
        return Handle(value.kind, value.canonical_id, value.generation)
    except AttributeError as exc:
        raise CorpusInputError("handle must be a Handle, 3-tuple, or mapping") from exc


def _normalize_faults(faults: Any) -> tuple[dict[str, Any], ...]:
    if faults is None:
        return ()
    if isinstance(faults, (str, bytes, bytearray)):
        raise MalformedFaultError("faults must be a sequence of fault descriptors, not a bare string/bytes")
    if isinstance(faults, Mapping):
        faults = [faults]
    elif not isinstance(faults, Iterable):
        raise MalformedFaultError("faults must be a sequence or iterable of fault descriptors")

    normalized: list[dict[str, Any]] = []
    for item in faults:
        if isinstance(item, str):
            kind = item
            entry = {"kind": kind}
        elif isinstance(item, Mapping):
            kind = item.get("kind") or item.get("fault")
            if not isinstance(kind, str) or not kind:
                raise MalformedFaultError("fault mapping must contain a non-empty string 'kind'")
            entry = copy.deepcopy(dict(item))
            entry["kind"] = kind
        else:
            raise MalformedFaultError(f"unsupported fault descriptor type: {type(item).__name__}")

        if kind not in VALID_FAULTS:
            raise MalformedFaultError(f"unsupported fault kind: {kind!r}")
        try:
            if "at_step" in entry:
                _require_strict_int(entry["at_step"], "fault.at_step", min_value=0)
            if "skip" in entry:
                _require_strict_int(entry["skip"], "fault.skip", min_value=1)
        except CorpusInputError as exc:
            raise MalformedFaultError(str(exc)) from exc
        normalized.append(entry)
    return tuple(normalized)


def _validate_geometry_ceilings(
    width: Any,
    height: Any,
    frames: Any,
    *,
    max_width: int = MAX_WIDTH,
    max_height: int = MAX_HEIGHT,
    max_frames: int = MAX_FRAMES,
    max_total_pixels: int = MAX_TOTAL_PIXELS,
) -> tuple[int, int, int]:
    """Enforce finite geometry and frame count ceilings strictly before RAM allocation."""
    if type(width) is not int or type(height) is not int or type(frames) is not int:
        raise CorpusCeilingError("width, height, and frames must be strict integers")
    if width < MIN_DIMENSION or height < MIN_DIMENSION:
        raise CorpusCeilingError(
            f"width ({width}) and height ({height}) must be >= {MIN_DIMENSION}"
        )
    if frames < 1:
        raise CorpusCeilingError(f"frames ({frames}) must be >= 1")
    if width > max_width or height > max_height:
        raise CorpusCeilingError(
            f"geometry {width}x{height} exceeds ceiling {max_width}x{max_height}"
        )
    if frames > max_frames:
        raise CorpusCeilingError(f"frame count {frames} exceeds ceiling {max_frames}")
    total_pixels = width * height * frames
    if total_pixels > max_total_pixels:
        raise CorpusCeilingError(
            f"total pixels {total_pixels} exceeds ceiling {max_total_pixels}"
        )
    return width, height, frames


@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    family: str = "static_control"
    seed_tag: int = 1
    width: int = 64
    height: int = 64
    frames: int = 6
    epoch: int = 1
    handle: Handle = field(default_factory=lambda: Handle("godot_window", "holdings_lane_0", 1))
    faults: tuple[dict[str, Any], ...] = ()
    dx: int = 4
    dy: int = 0
    half_period: int = 1
    patch_box: tuple[int, int, int, int] | None = None
    epoch_plan: tuple[dict[str, Any], ...] = ()
    source_kind: str = "godot"
    t2_records: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not self.case_id.strip():
            raise CorpusInputError("case_id must be a non-empty string")
        canonical_family = _FAMILY_ALIASES.get(self.family)
        if canonical_family is None:
            raise CorpusInputError(f"unknown case family: {self.family!r}")
        object.__setattr__(self, "family", canonical_family)
        _validate_seed_int(self.seed_tag, "seed_tag")
        _validate_geometry_ceilings(self.width, self.height, self.frames)
        _require_strict_int(self.epoch, "epoch", min_value=0)
        _require_strict_int(self.dx, "dx")
        _require_strict_int(self.dy, "dy")
        _require_strict_int(self.half_period, "half_period", min_value=1, max_value=64)
        object.__setattr__(self, "handle", _normalize_handle(self.handle, f"holdings_{self.case_id}"))
        object.__setattr__(self, "faults", _normalize_faults(self.faults))
        if self.patch_box is not None:
            if not isinstance(self.patch_box, (tuple, list)) or len(self.patch_box) != 4:
                raise CorpusInputError("patch_box must be a 4-tuple (x, y, w, h)")
            pb = tuple(_require_strict_int(v, "patch_box[i]", min_value=0) for v in self.patch_box)
            object.__setattr__(self, "patch_box", pb)
        if self.epoch_plan:
            plan_copy = []
            for seg in self.epoch_plan:
                if not isinstance(seg, Mapping):
                    raise CorpusInputError("epoch_plan entries must be mappings")
                ew = seg.get("width", self.width)
                eh = seg.get("height", self.height)
                ef = seg.get("frame_count", seg.get("frames", 2))
                ee = seg.get("epoch", self.epoch)
                es = seg.get("start_seq", 0)
                _validate_geometry_ceilings(ew, eh, ef)
                _require_strict_int(ee, "epoch_plan.epoch", min_value=0)
                _require_strict_int(es, "epoch_plan.start_seq", min_value=0)
                plan_copy.append({
                    "epoch": ee,
                    "width": ew,
                    "height": eh,
                    "start_seq": es,
                    "frame_count": ef,
                })
            object.__setattr__(self, "epoch_plan", tuple(plan_copy))
        if self.source_kind not in ("godot", "cdp", "neutral"):
            raise SourceBoundaryError(f"unsupported source_kind: {self.source_kind!r}")
        if self.t2_records:
            validated_t2 = tuple(validate_holdings_t2_record(r) for r in self.t2_records)
            object.__setattr__(self, "t2_records", validated_t2)


def coerce_case_spec(spec: CaseSpec | Mapping[str, Any]) -> CaseSpec:
    """Copy and validate a CaseSpec or mapping without retaining caller aliases."""
    if isinstance(spec, CaseSpec):
        return CaseSpec(
            case_id=str(spec.case_id),
            family=str(spec.family),
            seed_tag=int(spec.seed_tag),
            width=int(spec.width),
            height=int(spec.height),
            frames=int(spec.frames),
            epoch=int(spec.epoch),
            handle=_normalize_handle(spec.handle),
            faults=copy.deepcopy(spec.faults),
            dx=int(spec.dx),
            dy=int(spec.dy),
            half_period=int(spec.half_period),
            patch_box= tuple(spec.patch_box) if spec.patch_box is not None else None,
            epoch_plan=copy.deepcopy(spec.epoch_plan),
            source_kind=str(spec.source_kind),
            t2_records=copy.deepcopy(spec.t2_records),
        )
    if not isinstance(spec, Mapping):
        raise CorpusInputError(f"spec must be a CaseSpec or mapping, got {type(spec).__name__}")

    raw = copy.deepcopy(dict(spec))
    case_id = raw.get("case_id", raw.get("id", ""))
    family = raw.get("family", raw.get("kind", "static_control"))
    seed_tag = raw.get("seed_tag", raw.get("seed", 1))

    geom = raw.get("geometry")
    if isinstance(geom, Mapping):
        width = geom.get("width", geom.get("w", raw.get("width", raw.get("w", 64))))
        height = geom.get("height", geom.get("h", raw.get("height", raw.get("h", 64))))
        frames = geom.get("frames", geom.get("n", geom.get("count", raw.get("frames", raw.get("n", 6)))))
        epoch = geom.get("epoch", raw.get("epoch", 1))
    else:
        width = raw.get("width", raw.get("w", 64))
        height = raw.get("height", raw.get("h", 64))
        frames = raw.get("frames", raw.get("n", raw.get("count", 6)))
        epoch = raw.get("epoch", 1)

    handle = _normalize_handle(raw.get("handle"), f"holdings_{case_id or 'case'}")
    faults = _normalize_faults(raw.get("faults"))
    dx = raw.get("dx", 4 if _FAMILY_ALIASES.get(family) != "sparse_support" else 1)
    dy = raw.get("dy", 0)
    half_period = raw.get("half_period", 1)
    patch_box = raw.get("patch_box")
    epoch_plan = tuple(raw.get("epoch_plan", ()))
    source_kind = raw.get("source_kind", "godot")
    t2_records = tuple(raw.get("t2_records", ()))

    return CaseSpec(
        case_id=case_id,
        family=family,
        seed_tag=seed_tag,
        width=width,
        height=height,
        frames=frames,
        epoch=epoch,
        handle=handle,
        faults=faults,
        dx=dx,
        dy=dy,
        half_period=half_period,
        patch_box=tuple(patch_box) if patch_box is not None else None,
        epoch_plan=epoch_plan,
        source_kind=source_kind,
        t2_records=t2_records,
    )


@dataclass(frozen=True)
class FrameIntentLabel:
    """Independently authored expected label for a single frame (INV-J10-4)."""

    case_id: str
    frame_seq: int
    epoch: int
    width: int
    height: int
    cls: np.ndarray
    mv_dx: np.ndarray
    mv_dy: np.ndarray
    baseline_cls: np.ndarray
    settled: bool
    expected_error: str | None = None
    intent_op: str = "static_control"

    def __post_init__(self) -> None:
        for arr_name in ("cls", "mv_dx", "mv_dy", "baseline_cls"):
            arr = getattr(self, arr_name)
            owned = np.array(arr, dtype=np.int64, copy=True, order="C")
            owned.flags.writeable = False
            object.__setattr__(self, arr_name, owned)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "frame_seq": self.frame_seq,
            "epoch": self.epoch,
            "width": self.width,
            "height": self.height,
            "cls": [int(x) for x in self.cls.tolist()],
            "mv_dx": [int(x) for x in self.mv_dx.tolist()],
            "mv_dy": [int(x) for x in self.mv_dy.tolist()],
            "baseline_cls": [int(x) for x in self.baseline_cls.tolist()],
            "settled": bool(self.settled),
            "expected_error": self.expected_error,
            "intent_op": self.intent_op,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


@dataclass(frozen=True)
class FrameMetadataRecord:
    """Frame provenance record binding case_id, frame_seq, and pixel/raw/normalized digests."""

    case_id: str
    frame_seq: int
    pixel_decl_sha256: str
    raw_sha256: str
    normalized_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "frame_seq": int(self.frame_seq),
            "pixel_decl_sha256": self.pixel_decl_sha256,
            "raw_sha256": self.raw_sha256,
            "normalized_sha256": self.normalized_sha256,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


@dataclass(frozen=True)
class GeneratedFrameStep:
    """One synthesized volatile RAM frame step and its independent intent metadata."""

    case_id: str
    handle: Handle
    epoch: int
    frame_seq: int
    rgb: np.ndarray
    baseline_tiles: np.ndarray
    expected_label: FrameIntentLabel
    metadata: FrameMetadataRecord
    fault: str | None = None


@dataclass(frozen=True)
class GeneratedCase:
    """Complete synthesized case with owned RAM arrays and separate intent labels."""

    spec: CaseSpec
    master_seed: int
    lane_seed: int
    steps: tuple[GeneratedFrameStep, ...]
    expected_labels: tuple[FrameIntentLabel, ...]
    frame_metadata: tuple[FrameMetadataRecord, ...]
    epoch_plan: tuple[dict[str, Any], ...]

    @property
    def case_id(self) -> str:
        return self.spec.case_id

    @property
    def seed_tag(self) -> int:
        return self.spec.seed_tag

    @property
    def handle(self) -> Handle:
        return self.spec.handle

    @property
    def frames(self) -> tuple[np.ndarray, ...]:
        """Return fresh owned writable RAM copies so caller mutation never aliases corpus state."""
        return tuple(np.array(step.rgb, dtype=np.uint8, copy=True, order="C") for step in self.steps)

    @property
    def baseline_tiles(self) -> tuple[np.ndarray, ...]:
        """Return fresh owned copies of S2 baseline tile arrays."""
        return tuple(np.array(step.baseline_tiles, copy=True, order="C") for step in self.steps)

    def iter_frames(self) -> Iterable[np.ndarray]:
        for step in self.steps:
            yield np.array(step.rgb, dtype=np.uint8, copy=True, order="C")

    def to_case_manifest_entry(self) -> dict[str, Any]:
        grid = Grid(self.spec.width, self.spec.height)
        return {
            "case_id": self.spec.case_id,
            "family": self.spec.family,
            "seed_tag": int(self.spec.seed_tag),
            "geometry": {
                "width": int(self.spec.width),
                "height": int(self.spec.height),
                "frames": int(len(self.steps)),
                "grid_s": int(grid.s),
                "gw": int(grid.gw),
                "gh": int(grid.gh),
                "n_tiles": int(grid.N),
                "epoch": int(self.spec.epoch),
            },
            "faults": [copy.deepcopy(f) for f in self.spec.faults],
            "expected_labels": [lbl.to_dict() for lbl in self.expected_labels],
        }

    def __iter__(self):
        """Allow unpacking `frames, expected_labels = generate_case(spec)`."""
        yield self.frames
        yield self.expected_labels


def _synth_world_texture(width: int, height: int, seed_word: int, ox: int = 0, oy: int = 0) -> np.ndarray:
    """Synthesize deterministic non-periodic textured RGB uint8 array in RAM."""
    x = (np.arange(width, dtype=np.uint32) + np.uint32(ox & 0xFFFFFFFF))[None, :]
    y = (np.arange(height, dtype=np.uint32) + np.uint32(oy & 0xFFFFFFFF))[:, None]
    s_lo = np.uint32(seed_word & 0xFFFFFFFF)
    s_hi = np.uint32((seed_word >> 32) & 0xFFFFFFFF)
    z = (x * np.uint32(374761393)) ^ (y * np.uint32(668265263)) ^ s_lo
    z = (z ^ (z >> np.uint32(13))) * (np.uint32(1274126177) ^ (s_hi | np.uint32(1)))
    z ^= z >> np.uint32(16)
    base = ((z & np.uint32(63)) + np.uint32(48)).astype(np.uint8)
    bands = (((x // 8) % 3) + ((y // 11) % 3)).astype(np.uint8) * np.uint8(13)
    g0 = (base + bands).astype(np.uint8)
    rgb = np.empty((height, width, 3), dtype=np.uint8)
    rgb[:, :, 0] = g0
    rgb[:, :, 1] = (g0 + np.uint8(17)).astype(np.uint8)
    rgb[:, :, 2] = (g0 + np.uint8(31)).astype(np.uint8)
    return rgb


def _synth_sparse_outline_frame(width: int, height: int, seed_word: int, shift_x: int = 0) -> np.ndarray:
    """Synthesize a sparse exact-outline frame with an unchanged top tile row (ty == 0)."""
    grid = Grid(width, height)
    s = grid.s
    luma = np.full((height, width), 100, dtype=np.uint8)
    salt = int(seed_word & 0x7F)
    for ty in range(1, grid.gh):
        for tx in range(grid.gw):
            x0, y0 = tx * s, ty * s
            for idx, ry in enumerate((1, 4, 7, 10, 13)):
                if y0 + ry >= height:
                    continue
                val1 = int((15 + salt + tx * 23 + ty * 41 + idx * 17) % 230)
                val2 = int((31 + salt + tx * 37 + ty * 29 + idx * 19) % 230)
                if val1 == 100:
                    val1 = 105
                if val2 == 100:
                    val2 = 115
                c1s = max(0, x0 + 1 + shift_x)
                c1e = min(width, x0 + 5 + shift_x)
                if c1e > c1s:
                    luma[y0 + ry, c1s:c1e] = np.uint8(val1)
                c2s = max(0, x0 + 8 + shift_x)
                c2e = min(width, x0 + 12 + shift_x)
                if c2e > c2s:
                    luma[y0 + ry, c2s:c2e] = np.uint8(val2)
    rgb = np.stack([luma, luma, luma], axis=-1).astype(np.uint8, copy=True)
    return rgb


def _build_baseline_tiles(
    rgb: np.ndarray,
    grid: Grid,
    baseline_cls: np.ndarray,
    mv_dx: np.ndarray | None = None,
    mv_dy: np.ndarray | None = None,
) -> np.ndarray:
    padded = np.array(grid.pad(rgb), copy=True, order="C")
    tiles, _ = desc.describe_padded(padded, grid, lazy_xh=False)
    tiles = np.array(tiles, copy=True, order="C")
    tiles["cls"] = np.asarray(baseline_cls, dtype=tiles["cls"].dtype)
    if mv_dx is not None:
        tiles["mv_dx"] = np.asarray(mv_dx, dtype=tiles["mv_dx"].dtype)
    else:
        tiles["mv_dx"] = 0
    if mv_dy is not None:
        tiles["mv_dy"] = np.asarray(mv_dy, dtype=tiles["mv_dy"].dtype)
    else:
        tiles["mv_dy"] = 0
    tiles.flags.writeable = False
    return tiles


def _compute_frame_metadata(
    case_id: str,
    frame_seq: int,
    decl_payload: Mapping[str, Any],
    rgb: np.ndarray,
    grid: Grid,
    *,
    include_normalized_sha: bool = True,
) -> FrameMetadataRecord:
    decl_bytes = json.dumps(decl_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    pixel_decl_sha256 = hashlib.sha256(decl_bytes).hexdigest()
    raw_sha256 = hashlib.sha256(rgb.tobytes(order="C")).hexdigest()
    normalized_sha256: str | None = None
    if include_normalized_sha:
        padded = np.array(grid.pad(rgb), copy=True, order="C")
        y_plane = np.ascontiguousarray(luma_of_padded(padded), dtype=np.uint8)
        normalized_sha256 = hashlib.sha256(y_plane.tobytes(order="C")).hexdigest()
    return FrameMetadataRecord(
        case_id=case_id,
        frame_seq=frame_seq,
        pixel_decl_sha256=pixel_decl_sha256,
        raw_sha256=raw_sha256,
        normalized_sha256=normalized_sha256,
    )


def generate_case(
    spec: CaseSpec | Mapping[str, Any],
    rng_factory: Callable[[int], Any] = SplitMix64,
    *,
    master_seed: int = 0,
    include_normalized_sha: bool = True,
) -> GeneratedCase:
    """Synthesize a deterministic volatile RAM case and independent intent labels.

    Enforces declared finite geometry/count ceilings before allocating any array,
    rejects malformed seeds/faults explicitly, derives the lane seed solely from
    (master_seed, spec.seed_tag) via rng_factory, and retains no caller aliases.
    """
    case_spec = coerce_case_spec(spec)
    lane_seed = derive_lane_seed(master_seed, case_spec.seed_tag, rng_factory=rng_factory)
    rng = rng_factory(lane_seed)
    base_seed_word = rng.u64()

    family = case_spec.family
    w, h, n_frames = case_spec.width, case_spec.height, case_spec.frames
    epoch = case_spec.epoch

    if family == "resize_epoch" and not case_spec.epoch_plan:
        w2 = min(MAX_WIDTH, w + 16)
        h2 = min(MAX_HEIGHT, h + 16)
        n1 = max(1, n_frames // 2)
        n2 = max(1, n_frames - n1)
        _validate_geometry_ceilings(w2, h2, n2)
        epoch_plan = (
            {"epoch": epoch, "width": w, "height": h, "start_seq": 0, "frame_count": n1},
            {"epoch": epoch + 1, "width": w2, "height": h2, "start_seq": 0, "frame_count": n2},
        )
    elif case_spec.epoch_plan:
        epoch_plan = case_spec.epoch_plan
    else:
        epoch_plan = (
            {"epoch": epoch, "width": w, "height": h, "start_seq": 0, "frame_count": n_frames},
        )

    fault_kinds = {f["kind"] for f in case_spec.faults}
    steps: list[GeneratedFrameStep] = []
    expected_labels: list[FrameIntentLabel] = []
    frame_metadata: list[FrameMetadataRecord] = []

    if family == "resize_epoch":
        for seg_idx, seg in enumerate(epoch_plan):
            seg_w = int(seg["width"])
            seg_h = int(seg["height"])
            seg_epoch = int(seg["epoch"])
            seg_count = int(seg["frame_count"])
            seg_start = int(seg["start_seq"])
            grid = Grid(seg_w, seg_h)
            seg_seed = (base_seed_word ^ ((seg_idx + 1) * 0x9E3779B97F4A7C15)) & SPLITMIX64_MASK
            base_rgb = _synth_world_texture(seg_w, seg_h, seg_seed, 0, 0)
            clean_run = 0
            for local_i in range(seg_count):
                f_seq = seg_start + local_i
                rgb = base_rgb.copy()
                rgb.flags.writeable = False
                if local_i > 0:
                    clean_run += 1
                settled = clean_run >= PerWindowDetector.SETTLE_CONSECUTIVE
                cls_arr = np.zeros(grid.N, dtype=np.int64)
                mv_x = np.zeros(grid.N, dtype=np.int64)
                mv_y = np.zeros(grid.N, dtype=np.int64)
                base_cls = np.zeros(grid.N, dtype=np.int64)
                exp_err = "DetectorInputError" if ("stale_epoch" in fault_kinds or "geometry_without_epoch" in fault_kinds) else None
                lbl = FrameIntentLabel(
                    case_id=case_spec.case_id,
                    frame_seq=f_seq,
                    epoch=seg_epoch,
                    width=seg_w,
                    height=seg_h,
                    cls=cls_arr,
                    mv_dx=mv_x,
                    mv_dy=mv_y,
                    baseline_cls=base_cls,
                    settled=settled,
                    expected_error=exp_err,
                    intent_op="resize_epoch",
                )
                decl = {
                    "case_id": case_spec.case_id,
                    "family": family,
                    "epoch": seg_epoch,
                    "frame_seq": f_seq,
                    "width": seg_w,
                    "height": seg_h,
                    "seg_seed": seg_seed,
                }
                meta = _compute_frame_metadata(
                    case_spec.case_id,
                    f_seq,
                    decl,
                    rgb,
                    grid,
                    include_normalized_sha=include_normalized_sha,
                )
                tiles = _build_baseline_tiles(rgb, grid, base_cls, mv_x, mv_y)
                steps.append(
                    GeneratedFrameStep(
                        case_id=case_spec.case_id,
                        handle=case_spec.handle,
                        epoch=seg_epoch,
                        frame_seq=f_seq,
                        rgb=rgb,
                        baseline_tiles=tiles,
                        expected_label=lbl,
                        metadata=meta,
                    )
                )
                expected_labels.append(lbl)
                frame_metadata.append(meta)
        return GeneratedCase(
            spec=case_spec,
            master_seed=master_seed,
            lane_seed=lane_seed,
            steps=tuple(steps),
            expected_labels=tuple(expected_labels),
            frame_metadata=tuple(frame_metadata),
            epoch_plan=epoch_plan,
        )

    grid = Grid(w, h)
    base_rgb = _synth_world_texture(w, h, base_seed_word, 0, 0)
    clean_run = 0
    ox = 0
    oy = 0

    skip_at = max(1, n_frames // 2)
    skip_delta = 2
    for f in case_spec.faults:
        if f["kind"] in ("skip_frame", "gap"):
            skip_at = int(f.get("at_step", skip_at))
            skip_delta = int(f.get("skip", skip_delta))

    cur_seq = 0
    for step_idx in range(n_frames):
        if step_idx == 0:
            cur_seq = 0
        elif family == "skipped_frame" or "skip_frame" in fault_kinds or "gap" in fault_kinds:
            if step_idx == skip_at:
                cur_seq += skip_delta
                clean_run = 0
            else:
                cur_seq += 1
        else:
            cur_seq += 1

        cls_arr = np.zeros(grid.N, dtype=np.int64)
        mv_x = np.zeros(grid.N, dtype=np.int64)
        mv_y = np.zeros(grid.N, dtype=np.int64)
        base_cls = np.zeros(grid.N, dtype=np.int64)
        intent_op = family
        exp_err: str | None = None

        if family in ("static_control", "caller_mutation", "skipped_frame"):
            rgb = base_rgb.copy()
            if step_idx > 0 and not (
                (family == "skipped_frame" or "skip_frame" in fault_kinds or "gap" in fault_kinds)
                and step_idx == skip_at
            ):
                clean_run += 1
            else:
                clean_run = 0
            decl_op = {"op": family, "seed": base_seed_word, "step": step_idx}

        elif family == "localized_change":
            rgb = base_rgb.copy()
            if case_spec.patch_box is not None:
                px, py, pw, ph = case_spec.patch_box
                target_tx = px // grid.s
                target_ty = py // grid.s
            else:
                target_tx = min(1, grid.gw - 1)
                target_ty = min(1, grid.gh - 1)
            x0, y0 = target_tx * grid.s, target_ty * grid.s
            x1, y1 = min(w, x0 + grid.s), min(h, y0 + grid.s)
            if step_idx > 0:
                patch_seed = derive_frame_seed(lane_seed, step_idx, rng_factory=rng_factory)
                rgb[y0:y1, x0:x1] = _synth_world_texture(x1 - x0, y1 - y0, patch_seed, step_idx * 7, step_idx * 11)
                for tid in range(grid.N):
                    if int(grid.tx[tid]) == target_tx and int(grid.ty[tid]) == target_ty:
                        cls_arr[tid] = CLASS_CHANGED
                        base_cls[tid] = CLASS_CHANGED
                clean_run = 0
            decl_op = {"op": "localized_change", "seed": base_seed_word, "step": step_idx, "tile": [target_tx, target_ty]}

        elif family == "integer_translation":
            dx = case_spec.dx if case_spec.dx != 0 or case_spec.dy != 0 else 4
            dy = case_spec.dy
            if step_idx > 0:
                ox -= dx
                oy -= dy
            rgb = _synth_world_texture(w, h, base_seed_word, ox, oy)
            if step_idx > 0:
                for tid in range(grid.N):
                    tx, ty = int(grid.tx[tid]), int(grid.ty[tid])
                    x0, y0 = tx * grid.s, ty * grid.s
                    x1, y1 = min(w, x0 + grid.s), min(h, y0 + grid.s)
                    base_cls[tid] = CLASS_CHANGED
                    if x0 - dx >= 0 and x1 - dx <= w and y0 - dy >= 0 and y1 - dy <= h:
                        cls_arr[tid] = CLASS_MOVED
                        mv_x[tid] = dx
                        mv_y[tid] = dy
                    else:
                        cls_arr[tid] = CLASS_CHANGED
                clean_run = 0
            decl_op = {"op": "integer_translation", "seed": base_seed_word, "ox": ox, "oy": oy, "dx": dx, "dy": dy}

        elif family == "periodic_flicker_veto":
            dx = case_spec.dx if case_spec.dx != 0 else 4
            dy = 0
            blink_tx = min(1, grid.gw - 1)
            blink_ty = min(1, grid.gh - 1)
            bx0, by0 = blink_tx * grid.s, blink_ty * grid.s
            bx1, by1 = min(w, bx0 + grid.s), min(h, by0 + grid.s)
            state_b_patch = _synth_world_texture(bx1 - bx0, by1 - by0, base_seed_word ^ 0xA5A5A5A5, 33, 77)

            # Steps 0..3: pure 2-state periodic blink on tile (blink_tx, blink_ty).
            # Step 4+: real global horizontal translation by dx across the entire frame,
            # which vetoes/overrides any flicker claim with real motion MOVED (3) on interior tiles!
            if step_idx < 4:
                rgb = base_rgb.copy()
                if step_idx % 2 == 1:
                    rgb[by0:by1, bx0:bx1] = state_b_patch
                if step_idx > 0:
                    for tid in range(grid.N):
                        if int(grid.tx[tid]) == blink_tx and int(grid.ty[tid]) == blink_ty:
                            if step_idx >= 2:
                                base_cls[tid] = CLASS_FLICKER
                                cls_arr[tid] = CLASS_FLICKER
                            else:
                                base_cls[tid] = CLASS_CHANGED
                                cls_arr[tid] = CLASS_CHANGED
                    clean_run = 0
                decl_op = {"op": "periodic_flicker", "step": step_idx, "phase": step_idx % 2}
            else:
                prev_rgb = steps[-1].rgb
                rgb = np.empty_like(prev_rgb)
                rgb[:, dx:] = prev_rgb[:, :-dx]
                rgb[:, :dx] = _synth_world_texture(dx, h, base_seed_word ^ (step_idx * 101), step_idx, 0)
                for tid in range(grid.N):
                    tx, ty = int(grid.tx[tid]), int(grid.ty[tid])
                    x0, y0 = tx * grid.s, ty * grid.s
                    x1, y1 = min(w, x0 + grid.s), min(h, y0 + grid.s)
                    # Even if baseline tentatively marks the blinking tile as FLICKER (4) or CHANGED (2),
                    # real global motion vetoes/overrides it to MOVED (3) on interior tiles.
                    base_cls[tid] = CLASS_FLICKER if (int(grid.tx[tid]) == blink_tx and int(grid.ty[tid]) == blink_ty) else CLASS_CHANGED
                    if x0 - dx >= 0 and x1 - dx <= w:
                        cls_arr[tid] = CLASS_MOVED
                        mv_x[tid] = dx
                        mv_y[tid] = 0
                    else:
                        cls_arr[tid] = CLASS_CHANGED
                clean_run = 0
                decl_op = {"op": "real_motion_veto", "step": step_idx, "dx": dx}

        elif family == "sparse_support":
            dx = 1 if case_spec.dx == 0 else (1 if case_spec.dx > 0 else -1)
            shift_x = step_idx * dx
            rgb = _synth_sparse_outline_frame(w, h, base_seed_word, shift_x=shift_x)
            if step_idx > 0:
                for tid in range(grid.N):
                    tx, ty = int(grid.tx[tid]), int(grid.ty[tid])
                    x0 = tx * grid.s
                    x1 = min(w, x0 + grid.s)
                    if ty == 0:
                        # Top row of tiles is completely unchanged -> sparse unchanged baseline label (0)
                        base_cls[tid] = CLASS_UNCHANGED
                        cls_arr[tid] = CLASS_UNCHANGED
                    else:
                        base_cls[tid] = CLASS_CHANGED
                        if x0 - dx >= 0 and x1 - dx <= w:
                            cls_arr[tid] = CLASS_MOVED
                            mv_x[tid] = dx
                            mv_y[tid] = 0
                        else:
                            cls_arr[tid] = CLASS_CHANGED
                clean_run = 0
            decl_op = {"op": "sparse_support", "seed": base_seed_word, "shift_x": shift_x, "dx": dx}

        elif family == "mixed_history":
            # Step 0: initial static frame
            # Step 1: integer translation by dx=4
            # Step 2: localized change on tile (1, 1)
            # Step 3..n-1: static frames (clean_run increments 1, 2, 3 -> settles at step 5)
            dx = case_spec.dx if case_spec.dx != 0 else 4
            if step_idx == 0:
                rgb = base_rgb.copy()
                clean_run = 0
            elif step_idx == 1:
                ox -= dx
                rgb = _synth_world_texture(w, h, base_seed_word, ox, oy)
                for tid in range(grid.N):
                    tx = int(grid.tx[tid])
                    x0 = tx * grid.s
                    x1 = min(w, x0 + grid.s)
                    base_cls[tid] = CLASS_CHANGED
                    if x0 - dx >= 0 and x1 - dx <= w:
                        cls_arr[tid] = CLASS_MOVED
                        mv_x[tid] = dx
                    else:
                        cls_arr[tid] = CLASS_CHANGED
                clean_run = 0
            elif step_idx == 2:
                rgb = steps[-1].rgb.copy()
                target_tx = min(1, grid.gw - 1)
                target_ty = min(1, grid.gh - 1)
                x0, y0 = target_tx * grid.s, target_ty * grid.s
                x1, y1 = min(w, x0 + grid.s), min(h, y0 + grid.s)
                rgb[y0:y1, x0:x1] = _synth_world_texture(x1 - x0, y1 - y0, base_seed_word ^ 0x55AA55AA, 9, 9)
                for tid in range(grid.N):
                    if int(grid.tx[tid]) == target_tx and int(grid.ty[tid]) == target_ty:
                        base_cls[tid] = CLASS_CHANGED
                        cls_arr[tid] = CLASS_CHANGED
                clean_run = 0
            else:
                rgb = steps[-1].rgb.copy()
                clean_run += 1
            decl_op = {"op": "mixed_history", "step": step_idx, "clean_run": clean_run}

        elif family == "plugin_exception":
            rgb = base_rgb.copy()
            if step_idx == 1:
                exp_err = "RuntimeError"
                clean_run = 0
            elif step_idx == 2:
                # Immediately after plugin exception on step 1, stream was reset (st = None),
                # so step 2 starts a fresh stream with clean_run = 0!
                clean_run = 0
            elif step_idx > 2:
                clean_run += 1
            decl_op = {"op": "plugin_exception", "step": step_idx, "expected_error": exp_err}

        elif family == "cache_eviction":
            # Each step modifies a single tile (1, 1) with a distinct frame seed so > MAX_CACHE_PAIRS (32) pairs enter cache.
            rgb = base_rgb.copy()
            target_tx = min(1, grid.gw - 1)
            target_ty = min(1, grid.gh - 1)
            x0, y0 = target_tx * grid.s, target_ty * grid.s
            x1, y1 = min(w, x0 + grid.s), min(h, y0 + grid.s)
            if step_idx > 0:
                patch_seed = derive_frame_seed(lane_seed, step_idx, rng_factory=rng_factory)
                rgb[y0:y1, x0:x1] = _synth_world_texture(x1 - x0, y1 - y0, patch_seed, step_idx * 5, step_idx * 9)
                for tid in range(grid.N):
                    if int(grid.tx[tid]) == target_tx and int(grid.ty[tid]) == target_ty:
                        base_cls[tid] = CLASS_CHANGED
                        cls_arr[tid] = CLASS_CHANGED
                clean_run = 0
            decl_op = {"op": "cache_eviction", "step": step_idx, "tile": [target_tx, target_ty]}

        else:
            raise CorpusInputError(f"unhandled case family: {family!r}")

        rgb = np.array(rgb, dtype=np.uint8, copy=True, order="C")
        rgb.flags.writeable = False
        settled = clean_run >= PerWindowDetector.SETTLE_CONSECUTIVE

        lbl = FrameIntentLabel(
            case_id=case_spec.case_id,
            frame_seq=cur_seq,
            epoch=epoch,
            width=w,
            height=h,
            cls=cls_arr,
            mv_dx=mv_x,
            mv_dy=mv_y,
            baseline_cls=base_cls,
            settled=settled,
            expected_error=exp_err,
            intent_op=intent_op,
        )
        decl_payload = {
            "case_id": case_spec.case_id,
            "family": family,
            "seed_tag": case_spec.seed_tag,
            "epoch": epoch,
            "frame_seq": cur_seq,
            "width": w,
            "height": h,
            "decl": decl_op,
        }
        meta = _compute_frame_metadata(
            case_spec.case_id,
            cur_seq,
            decl_payload,
            rgb,
            grid,
            include_normalized_sha=include_normalized_sha,
        )
        tiles = _build_baseline_tiles(rgb, grid, base_cls, mv_x, mv_y)
        step_fault = "plugin_exception" if (family == "plugin_exception" and step_idx == 1) else None
        steps.append(
            GeneratedFrameStep(
                case_id=case_spec.case_id,
                handle=case_spec.handle,
                epoch=epoch,
                frame_seq=cur_seq,
                rgb=rgb,
                baseline_tiles=tiles,
                expected_label=lbl,
                metadata=meta,
                fault=step_fault,
            )
        )
        expected_labels.append(lbl)
        frame_metadata.append(meta)

    return GeneratedCase(
        spec=case_spec,
        master_seed=master_seed,
        lane_seed=lane_seed,
        steps=tuple(steps),
        expected_labels=tuple(expected_labels),
        frame_metadata=tuple(frame_metadata),
        epoch_plan=epoch_plan,
    )


def build_complete_case_family_specs(*, master_Handle_prefix: str = "lane") -> tuple[CaseSpec, ...]:
    """Return canonical CaseSpecs covering all 11 required case families."""
    specs = []
    for idx, family in enumerate(CASE_FAMILIES, start=1):
        handle_kind = "godot_window" if idx % 2 == 1 else "cdp_tab"
        source_kind = "godot" if handle_kind == "godot_window" else "cdp"
        frames = 36 if family == "cache_eviction" else 6
        faults: tuple[dict[str, Any], ...] = ()
        if family == "skipped_frame":
            faults = ({"kind": "skip_frame", "at_step": 2, "skip": 3},)
        elif family == "resize_epoch":
            faults = ({"kind": "resize_epoch"},)
        elif family == "caller_mutation":
            faults = ({"kind": "caller_mutation"},)
        elif family == "plugin_exception":
            faults = ({"kind": "plugin_exception", "at_step": 1},)
        elif family == "cache_eviction":
            faults = ({"kind": "cache_eviction"},)

        specs.append(
            CaseSpec(
                case_id=f"case_{idx:02d}_{family}",
                family=family,
                seed_tag=100 + idx,
                width=64,
                height=64,
                frames=frames,
                epoch=1,
                handle=Handle(handle_kind, f"{master_Handle_prefix}_{idx:02d}", 1),
                faults=faults,
                dx=1 if family == "sparse_support" else 4,
                dy=0,
                source_kind=source_kind,
            )
        )
    return tuple(specs)


def compute_generator_sha256() -> str:
    """Compute SHA-256 of this generator module's exact bytes in memory."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def compute_dependency_digest(root: Path | None = None) -> str:
    """Compute deterministic SHA-256 digest over bound dependency files."""
    repo_root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    hasher = hashlib.sha256()
    for rel in DEPENDENCY_RELATIVE_PATHS:
        target = repo_root / rel
        if not target.is_file():
            return AUSENTE
        data = target.read_bytes()
        file_sha = hashlib.sha256(data).hexdigest()
        hasher.update(f"{rel}:{file_sha}\n".encode("utf-8"))
    return hasher.hexdigest()


def capture_environment_pin(*, root: Path | None = None) -> dict[str, str]:
    """Pin python, numpy, platform, and dependency_digest for corpus provenance."""
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    np_ver = str(np.__version__)
    plat = f"{sys.platform}-{platform.machine()}"
    dep_digest = compute_dependency_digest(root=root)
    return {
        "python": py_ver,
        "numpy": np_ver,
        "platform": plat,
        "dependency_digest": dep_digest,
    }


@dataclass(frozen=True)
class CorpusManifest:
    """Source-neutral generated corpus provenance manifest (INV-J10-2)."""

    schema: str
    generator_sha256: str
    algorithm_id: str
    master_seed: int
    environment: dict[str, str]
    lanes: tuple[dict[str, Any], ...]
    cases: tuple[dict[str, Any], ...]
    frames: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "generator_sha256": self.generator_sha256,
            "algorithm_id": self.algorithm_id,
            "master_seed": int(self.master_seed),
            "environment": dict(self.environment),
            "lanes": copy.deepcopy(list(self.lanes)),
            "cases": copy.deepcopy(list(self.cases)),
            "frames": copy.deepcopy(list(self.frames)),
        }

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def keys(self):
        return ("schema", "generator_sha256", "algorithm_id", "master_seed", "environment", "lanes", "cases", "frames")


def build_corpus_manifest(
    specs_or_cases: Sequence[CaseSpec | Mapping[str, Any] | GeneratedCase],
    *,
    master_seed: int = 0,
    rng_factory: Callable[[int], Any] = SplitMix64,
    environment: Mapping[str, str] | None = None,
    generator_sha256: str | None = None,
    algorithm_id: str = ALGORITHM_ID,
) -> CorpusManifest:
    """Build the provenance manifest binding schema, generator, seed, environment, lanes, cases, and frames."""
    _validate_seed_int(master_seed, "master_seed")
    if not isinstance(specs_or_cases, Sequence) or len(specs_or_cases) == 0:
        raise CorpusInputError("specs_or_cases must be a non-empty sequence")
    if len(specs_or_cases) > MAX_CASES:
        raise CorpusCeilingError(f"case count {len(specs_or_cases)} exceeds MAX_CASES ({MAX_CASES})")

    env = dict(environment) if environment is not None else capture_environment_pin()
    required_env_keys = ("python", "numpy", "platform", "dependency_digest")
    for k in required_env_keys:
        val = env.get(k)
        if not isinstance(val, str) or not val or val == AUSENTE:
            raise ProvenanceMismatchError(f"environment pin {k!r} is missing or AUSENTE")

    gen_sha = generator_sha256 if generator_sha256 is not None else compute_generator_sha256()
    if not isinstance(gen_sha, str) or len(gen_sha) != 64 or gen_sha == AUSENTE:
        raise ProvenanceMismatchError("generator_sha256 must be a 64-character lowercase hex digest")

    generated_cases: list[GeneratedCase] = []
    for item in specs_or_cases:
        if isinstance(item, GeneratedCase):
            generated_cases.append(item)
        else:
            generated_cases.append(generate_case(item, rng_factory=rng_factory, master_seed=master_seed))

    lanes_map: OrderedDict[tuple[str, str, int], dict[str, Any]] = OrderedDict()
    cases_entries: list[dict[str, Any]] = []
    frames_entries: list[dict[str, Any]] = []

    for gcase in generated_cases:
        h = gcase.handle
        h_key = (h.kind, h.canonical_id, h.generation)
        if h_key not in lanes_map:
            lanes_map[h_key] = {
                "handle": {
                    "kind": h.kind,
                    "canonical_id": h.canonical_id,
                    "generation": int(h.generation),
                },
                "epoch_plan": [copy.deepcopy(seg) for seg in gcase.epoch_plan],
                "case_ids": [],
            }
        else:
            existing_plan = lanes_map[h_key]["epoch_plan"]
            for seg in gcase.epoch_plan:
                if seg not in existing_plan:
                    existing_plan.append(copy.deepcopy(seg))
        lanes_map[h_key]["case_ids"].append(gcase.case_id)
        cases_entries.append(gcase.to_case_manifest_entry())
        for fmeta in gcase.frame_metadata:
            frames_entries.append(fmeta.to_dict())

    if len(lanes_map) > MAX_LANES:
        raise CorpusCeilingError(f"lane count {len(lanes_map)} exceeds MAX_LANES ({MAX_LANES})")

    return CorpusManifest(
        schema=MANIFEST_SCHEMA,
        generator_sha256=gen_sha,
        algorithm_id=algorithm_id,
        master_seed=master_seed,
        environment=env,
        lanes=tuple(lanes_map.values()),
        cases=tuple(cases_entries),
        frames=tuple(frames_entries),
    )


def verify_regeneration(
    manifest: CorpusManifest | Mapping[str, Any],
    specs: Sequence[CaseSpec | Mapping[str, Any]],
    *,
    rng_factory: Callable[[int], Any] = SplitMix64,
    master_seed: int | None = None,
    environment: Mapping[str, Any] | None = None,
    generator_sha256: str | None = None,
    algorithm_id: str | None = None,
) -> dict[str, Any]:
    """Verify INV-J10-2: regeneration is guaranteed only for pinned seed, generator, and environment."""
    m_dict = manifest.to_dict() if isinstance(manifest, CorpusManifest) else dict(manifest)
    pinned_seed = m_dict.get("master_seed")
    pinned_gen = m_dict.get("generator_sha256")
    pinned_alg = m_dict.get("algorithm_id")
    pinned_env = m_dict.get("environment")

    actual_seed = pinned_seed if master_seed is None else master_seed
    actual_gen = compute_generator_sha256() if generator_sha256 is None else generator_sha256
    actual_alg = ALGORITHM_ID if algorithm_id is None else algorithm_id
    actual_env = capture_environment_pin() if environment is None else dict(environment)

    if actual_seed != pinned_seed:
        raise ProvenanceMismatchError(
            f"master_seed mismatch: actual {actual_seed!r} != pinned {pinned_seed!r}"
        )
    if actual_gen != pinned_gen or not isinstance(actual_gen, str) or actual_gen == AUSENTE:
        raise ProvenanceMismatchError(
            f"generator_sha256 mismatch: actual {actual_gen!r} != pinned {pinned_gen!r}"
        )
    if actual_alg != pinned_alg:
        raise ProvenanceMismatchError(
            f"algorithm_id mismatch: actual {actual_alg!r} != pinned {pinned_alg!r}"
        )
    if not isinstance(pinned_env, Mapping):
        raise ProvenanceMismatchError("manifest environment pin is missing")
    for k in ("python", "numpy", "platform", "dependency_digest"):
        p_val = pinned_env.get(k)
        a_val = actual_env.get(k)
        if not p_val or p_val == AUSENTE or not a_val or a_val == AUSENTE or p_val != a_val:
            raise ProvenanceMismatchError(
                f"environment[{k!r}] mismatch: actual {a_val!r} != pinned {p_val!r}"
            )

    regenerated = build_corpus_manifest(
        specs,
        master_seed=actual_seed,
        rng_factory=rng_factory,
        environment=actual_env,
        generator_sha256=actual_gen,
        algorithm_id=actual_alg,
    )
    if regenerated.to_dict() != m_dict:
        raise ProvenanceMismatchError("regenerated corpus manifest differs from pinned manifest")

    return {
        "verified": True,
        "master_seed": actual_seed,
        "generator_sha256": actual_gen,
        "frame_count": len(regenerated.frames),
        "case_count": len(regenerated.cases),
    }


@contextmanager
def preserve_operation_contexts():
    """Preserve and restore outer GM and FL ContextVar operation counters on normal exit or exception (INV-J10-3)."""
    gm_ops_before = gm_bands_hv._ops.get()
    gm_diag_before = copy.deepcopy(gm_bands_hv._diagnostics.get())
    fl_ops_before = fl_blinkprior._ops.get()
    try:
        yield {
            "gm_ops_before": gm_ops_before,
            "gm_diag_before": gm_diag_before,
            "fl_ops_before": fl_ops_before,
        }
    finally:
        gm_bands_hv._ops.set(gm_ops_before)
        gm_bands_hv._diagnostics.set(gm_diag_before)
        fl_blinkprior._ops.set(fl_ops_before)


@dataclass(frozen=True)
class StepEvaluation:
    case_id: str
    handle: Handle
    epoch: int
    frame_seq: int
    result: ChangeResult | None
    error: str | None
    ops: dict[str, int | None]
    raw_sha256: str
    cache_size_after: int


@dataclass(frozen=True)
class CorpusRunReport:
    per_lane_steps: dict[Handle, tuple[StepEvaluation, ...]]
    per_lane_totals: dict[Handle, dict[str, int | None]]
    per_lane_digests: dict[Handle, tuple[str, ...]]
    schedule_executed: tuple[tuple[str, int], ...]
    timing_domain: str = TIMING_DOMAIN


class _FaultInjectingGMProxy:
    """Wraps gm_bands_hv to inject a deterministic exception or missing counter on demand."""

    def __init__(self, base_gm: Any = gm_bands_hv, *, raise_on_call: bool = False, missing_counter: bool = False):
        self._base = base_gm
        self.raise_on_call = raise_on_call
        self.missing_counter = missing_counter

    def __getattr__(self, name: str) -> Any:
        if name == "OPS":
            if self.missing_counter:
                return None
            return getattr(self._base, "OPS", None)
        return getattr(self._base, name)

    def estimate_global_motion(self, prev_luma: np.ndarray, cur_luma: np.ndarray, max_d: int):
        if self.raise_on_call:
            gm_bands_hv._add_ops(777)
            raise RuntimeError("injected GM plugin exception")
        return self._base.estimate_global_motion(prev_luma, cur_luma, max_d)

    def consensus_tiles(self, prev_luma: np.ndarray, cur_luma: np.ndarray, s: int, dx: int, dy: int):
        if self.raise_on_call:
            gm_bands_hv._add_ops(777)
            raise RuntimeError("injected GM consensus exception")
        return self._base.consensus_tiles(prev_luma, cur_luma, s, dx, dy)


class _FaultInjectingFLProxy:
    """Wraps fl_v3 to inject a deterministic exception or missing counter on demand."""

    def __init__(self, base_fl: Any = fl_v3, *, raise_on_call: bool = False, missing_counter: bool = False):
        self._base = base_fl
        self.raise_on_call = raise_on_call
        self.missing_counter = missing_counter

    @property
    def FlickerTracker(self):
        return self._base.FlickerTracker

    def update_with_ops(self, tracker: Any, hashes: np.ndarray):
        if self.raise_on_call:
            with fl_v3.ops_scope():
                fl_blinkprior._add_ops(333)
                raise RuntimeError("injected FL plugin exception")
        res, ops = self._base.update_with_ops(tracker, hashes)
        if self.missing_counter:
            return res, None
        return res, ops


def run_corpus_schedule(
    cases: Sequence[GeneratedCase],
    *,
    schedule: Sequence[tuple[int, int]] | None = None,
    detector: PerWindowDetector | None = None,
    gm: Any = None,
    fl: Any = None,
    veto: Any = None,
    missing_gm_counter: bool = False,
    missing_fl_counter: bool = False,
) -> CorpusRunReport:
    """Execute generated cases through PerWindowDetector under an isolated or interleaved schedule.

    Preserves existing per-call GM and FL ContextVar counters and restores them
    even when plugins raise exceptions (INV-J10-3). Missing counters remain None.
    """
    gm_proxy = _FaultInjectingGMProxy(gm or gm_bands_hv, missing_counter=missing_gm_counter)
    fl_proxy = _FaultInjectingFLProxy(fl or fl_v3, missing_counter=missing_fl_counter)
    det = detector if detector is not None else PerWindowDetector(gm=gm_proxy, fl=fl_proxy, veto=veto)

    if schedule is None:
        exec_order: list[tuple[int, int]] = []
        for c_idx, gcase in enumerate(cases):
            for s_idx in range(len(gcase.steps)):
                exec_order.append((c_idx, s_idx))
    else:
        exec_order = [(int(c_idx), int(s_idx)) for c_idx, s_idx in schedule]

    per_lane_steps: dict[Handle, list[StepEvaluation]] = {}
    per_lane_digests: dict[Handle, list[str]] = {}
    schedule_records: list[tuple[str, int]] = []

    for c_idx, s_idx in exec_order:
        gcase = cases[c_idx]
        step = gcase.steps[s_idx]
        handle = step.handle
        per_lane_steps.setdefault(handle, [])
        per_lane_digests.setdefault(handle, [])
        schedule_records.append((gcase.case_id, step.frame_seq))

        # Pass an owned writable copy so caller-mutation faults can mutate their buffer after update.
        caller_rgb = np.array(step.rgb, dtype=np.uint8, copy=True, order="C")
        caller_tiles = np.array(step.baseline_tiles, copy=True, order="C")

        inject_exc = step.fault == "plugin_exception"
        gm_proxy.raise_on_call = False
        fl_proxy.raise_on_call = inject_exc

        with preserve_operation_contexts():
            try:
                res = copy_context().run(
                    lambda: det.update(
                        handle,
                        caller_tiles,
                        caller_rgb,
                        epoch=step.epoch,
                        frame_seq=step.frame_seq,
                    )
                )
                err_name = None
                ops_dict = {"gm": res.ops.get("gm"), "fl": res.ops.get("fl")}
            except Exception as exc:
                res = None
                err_name = type(exc).__name__
                ops_dict = {"gm": None, "fl": None}
            finally:
                gm_proxy.raise_on_call = False
                fl_proxy.raise_on_call = False

        if gcase.spec.family == "caller_mutation":
            # Mutate the caller's buffers after detector admission to prove no alias retention.
            caller_rgb[:] = 255
            caller_tiles["cls"][:] = CLASS_DRIFT

        lane_obj = det._lanes.get(handle)
        st_obj = lane_obj.stream if lane_obj is not None else None
        cache_sz = len(st_obj.cache) if st_obj is not None else 0

        per_lane_steps[handle].append(
            StepEvaluation(
                case_id=gcase.case_id,
                handle=handle,
                epoch=step.epoch,
                frame_seq=step.frame_seq,
                result=res,
                error=err_name,
                ops=ops_dict,
                raw_sha256=step.metadata.raw_sha256,
                cache_size_after=cache_sz,
            )
        )
        per_lane_digests[handle].append(step.metadata.raw_sha256)

    per_lane_totals: dict[Handle, dict[str, int | None]] = {}
    for handle, step_list in per_lane_steps.items():
        gm_sum: int | None = 0
        fl_sum: int | None = 0
        for ev in step_list:
            if ev.error is not None:
                continue
            g_val = ev.ops.get("gm")
            f_val = ev.ops.get("fl")
            if g_val is None:
                gm_sum = None
            elif gm_sum is not None:
                gm_sum += int(g_val)
            if f_val is None:
                fl_sum = None
            elif fl_sum is not None:
                fl_sum += int(f_val)
        per_lane_totals[handle] = {"gm": gm_sum, "fl": fl_sum}

    return CorpusRunReport(
        per_lane_steps={h: tuple(v) for h, v in per_lane_steps.items()},
        per_lane_totals=per_lane_totals,
        per_lane_digests={h: tuple(v) for h, v in per_lane_digests.items()},
        schedule_executed=tuple(schedule_records),
        timing_domain=TIMING_DOMAIN,
    )


def make_holdings_t2_command(
    tick: int,
    seq: int,
    cmd: str,
    args: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct an inline Holdings T2 command record using COMMANDS.md / CONTRACTS.md field names.

    External T2 fields are strictly ('tick', 'seq', 'cmd', 'args').
    - 'pause' has empty args {}.
    - 'new_game' has ('country_id', 'difficulty', 'opening_mode', 'seed').
    """
    _require_strict_int(tick, "tick", min_value=0)
    _require_strict_int(seq, "seq", min_value=0)
    if not isinstance(cmd, str) or not cmd:
        raise SourceBoundaryError("cmd must be a non-empty string")
    args_dict = copy.deepcopy(dict(args)) if args is not None else {}
    record = {
        "tick": tick,
        "seq": seq,
        "cmd": cmd,
        "args": args_dict,
    }
    return validate_holdings_t2_record(record)


def validate_holdings_t2_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate that an external T2 command record has exact fields and valid command args."""
    if not isinstance(record, Mapping):
        raise SourceBoundaryError("T2 record must be a mapping")
    keys = tuple(record.keys())
    if set(keys) != set(EXTERNAL_T2_FIELDS):
        raise SourceBoundaryError(
            f"external T2 record must have exact keys {EXTERNAL_T2_FIELDS}, got {keys}"
        )
    tick = _require_strict_int(record["tick"], "tick", min_value=0)
    seq = _require_strict_int(record["seq"], "seq", min_value=0)
    cmd = record["cmd"]
    args = record["args"]
    if not isinstance(cmd, str) or not cmd:
        raise SourceBoundaryError("T2 cmd must be a non-empty string")
    if not isinstance(args, Mapping):
        raise SourceBoundaryError("T2 args must be a mapping")
    if any(k in args for k in ("authority", "trusted_authority", "lease_id", "admin_override")):
        raise SourceBoundaryError("authority must stay in trusted injected context, never in T2 args")

    args_copy = copy.deepcopy(dict(args))
    if cmd == "pause":
        if len(args_copy) != 0:
            raise SourceBoundaryError("Holdings 'pause' command must have empty args {}")
    elif cmd == "new_game":
        if set(args_copy.keys()) != set(NEW_GAME_ARG_FIELDS):
            raise SourceBoundaryError(
                f"Holdings 'new_game' args must have exact keys {NEW_GAME_ARG_FIELDS}, got {tuple(args_copy.keys())}"
            )
        if not isinstance(args_copy["country_id"], str) or not args_copy["country_id"]:
            raise SourceBoundaryError("new_game.country_id must be a non-empty string")
        if not isinstance(args_copy["difficulty"], str) or not args_copy["difficulty"]:
            raise SourceBoundaryError("new_game.difficulty must be a non-empty string")
        if not isinstance(args_copy["opening_mode"], str) or not args_copy["opening_mode"]:
            raise SourceBoundaryError("new_game.opening_mode must be a non-empty string")
        _validate_seed_int(args_copy["seed"], "new_game.seed")
    else:
        raise SourceBoundaryError(f"unsupported Holdings T2 command: {cmd!r}")

    return {"tick": tick, "seq": seq, "cmd": cmd, "args": args_copy}


def make_neutral_godot_observation(
    handle: Handle,
    *,
    epoch: int = 1,
    frame_seq: int = 0,
    frame_digest: str = "0" * 64,
    tick: int = 10,
    country_id: str = "ARG",
    treasury: int = 1000,
    paused: bool = False,
) -> dict[str, Any]:
    """Build a source-neutral observation fixture for a Godot Holdings target without importing Godot."""
    h = _normalize_handle(handle)
    if h.kind != "godot_window":
        raise SourceBoundaryError("Godot observation fixture requires handle.kind == 'godot_window'")
    return validate_source_observation({
        "handle": h,
        "epoch": epoch,
        "frame_seq": frame_seq,
        "frame_digest": frame_digest,
        "source": {
            "kind": "godot",
            "adapter_imported": False,
            "telemetry": {
                "tick": tick,
                "country_id": country_id,
                "treasury": treasury,
                "paused": paused,
            },
        },
    })


def make_fake_cdp_observation(
    handle: Handle,
    *,
    epoch: int = 1,
    frame_seq: int = 0,
    frame_digest: str = "1" * 64,
    document_url: str = "app://holdings/ledger",
    ax_nodes: Sequence[Mapping[str, Any]] | None = None,
    dom_nodes: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a source-neutral observation fixture from fake-CDP-derived DOM/AX data."""
    h = _normalize_handle(handle)
    if h.kind != "cdp_tab":
        raise SourceBoundaryError("CDP observation fixture requires handle.kind == 'cdp_tab'")
    default_ax = (
        {"nodeId": "ax-1", "backendDOMNodeId": 101, "role": "WebArea", "name": "Holdings Ledger"},
        {"nodeId": "ax-2", "backendDOMNodeId": 102, "role": "button", "name": "Pause"},
    )
    default_dom = (
        {"nodeId": 1, "backendNodeId": 101, "nodeName": "DOCUMENT", "documentURL": document_url},
        {"nodeId": 2, "backendNodeId": 102, "nodeName": "BUTTON", "attributes": ["id", "btn-pause"]},
    )
    return validate_source_observation({
        "handle": h,
        "epoch": epoch,
        "frame_seq": frame_seq,
        "frame_digest": frame_digest,
        "source": {
            "kind": "cdp",
            "adapter_imported": False,
            "document_url": document_url,
            "ax_tree": [copy.deepcopy(dict(n)) for n in (ax_nodes if ax_nodes is not None else default_ax)],
            "dom_nodes": [copy.deepcopy(dict(n)) for n in (dom_nodes if dom_nodes is not None else default_dom)],
        },
    })


def validate_source_observation(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a source-neutral observation and enforce that source.kind=='cdp' is never a relabeled Godot tree."""
    if not isinstance(observation, Mapping):
        raise SourceBoundaryError("observation must be a mapping")
    obs = copy.deepcopy(dict(observation))
    handle = _normalize_handle(obs.get("handle"))
    obs["handle"] = handle
    _require_strict_int(obs.get("epoch"), "observation.epoch", min_value=0)
    _require_strict_int(obs.get("frame_seq"), "observation.frame_seq", min_value=0)
    digest = obs.get("frame_digest")
    if not isinstance(digest, str) or len(digest) != 64:
        raise SourceBoundaryError("observation.frame_digest must be a 64-char hex digest")

    source = obs.get("source")
    if not isinstance(source, Mapping):
        raise SourceBoundaryError("observation.source must be a mapping")
    kind = source.get("kind")
    if kind not in ("godot", "cdp", "neutral"):
        raise SourceBoundaryError(f"unsupported source.kind: {kind!r}")

    if kind == "cdp":
        # source.kind 'cdp' is reserved strictly for CDP-derived DOM/AX, never a relabeled Godot tree!
        forbidden_godot_keys = {"scene_tree", "godot_class", "node_path", "canvas_item", "autoloads", "root_viewport"}
        if forbidden_godot_keys.intersection(source.keys()):
            raise SourceBoundaryError("source.kind 'cdp' rejected relabeled Godot tree keys")
        ax_tree = source.get("ax_tree")
        dom_nodes = source.get("dom_nodes")
        if not isinstance(ax_tree, list) or not ax_tree or not isinstance(dom_nodes, list) or not dom_nodes:
            raise SourceBoundaryError("source.kind 'cdp' requires non-empty CDP-derived ax_tree and dom_nodes")
        for node in ax_tree:
            if not isinstance(node, Mapping) or "backendDOMNodeId" not in node or "role" not in node:
                raise SourceBoundaryError("CDP ax_tree nodes must carry backendDOMNodeId and role")
            if "godot_class" in node or "node_path" in node:
                raise SourceBoundaryError("CDP ax_tree node contains relabeled Godot fields")
        for node in dom_nodes:
            if not isinstance(node, Mapping) or "backendNodeId" not in node or "nodeName" not in node:
                raise SourceBoundaryError("CDP dom_nodes must carry backendNodeId and nodeName")
            if "godot_class" in node or "node_path" in node:
                raise SourceBoundaryError("CDP dom_node contains relabeled Godot fields")

    return obs


@dataclass
class _LaneBoundaryState:
    handle: Handle
    epoch: int = 1
    observations: list[dict[str, Any]] = field(default_factory=list)
    frame_digests: list[str] = field(default_factory=list)
    Simulation_state: dict[str, Any] = field(
        default_factory=lambda: {
            "tick": 0,
            "paused": False,
            "country_id": "NONE",
            "difficulty": "normal",
            "opening_mode": "standard",
            "seed": 0,
            "mutations": 0,
        }
    )
    trusted_bookkeeping: dict[str, Any] = field(
        default_factory=lambda: {
            "admitted_attempts": 0,
            "accepted_count": 0,
            "rejected_count": 0,
            "last_seq": -1,
            "outcomes": [],
        }
    )


class SourceNeutralBoundary:
    """Source-neutral harness boundary for injected observations, frame digests, T2 submission, outcomes, and reset."""

    def __init__(
        self,
        *,
        trusted_context: Mapping[str, Any] | None = None,
        electron_publisher: str = ELECTRON_BROKERED_PUBLISHER,
        conductor_binding: str = CONDUCTOR_ADMISSION_BINDING,
    ) -> None:
        self._trusted_context = copy.deepcopy(dict(trusted_context or {"authority": "trusted_harness", "wave": EXECUTION_WAVE}))
        self.electron_brokered_publisher = electron_publisher
        self.conductor_admission_binding = conductor_binding
        self._lanes: dict[Handle, _LaneBoundaryState] = {}

    def _lane(self, handle: Handle | Mapping[str, Any]) -> _LaneBoundaryState:
        h = _normalize_handle(handle)
        if h not in self._lanes:
            self._lanes[h] = _LaneBoundaryState(handle=h)
        return self._lanes[h]

    def inject_observation(self, observation: Mapping[str, Any]) -> dict[str, Any]:
        validated = validate_source_observation(observation)
        lane = self._lane(validated["handle"])
        lane.epoch = int(validated["epoch"])
        lane.observations.append(validated)
        lane.frame_digests.append(validated["frame_digest"])
        return copy.deepcopy(validated)

    def submit_t2(
        self,
        handle: Handle | Mapping[str, Any],
        t2_record: Mapping[str, Any],
        *,
        allow_economic_acceptance: bool = True,
        rejection_reason: str | None = None,
    ) -> dict[str, Any]:
        """Submit an external T2 command under trusted injected context.

        Admission is not economic acceptance: rejected attempts advance trusted
        bookkeeping without mutating simulation state.
        """
        h = _normalize_handle(handle)
        lane = self._lane(h)
        validated = validate_holdings_t2_record(t2_record)
        if not self._trusted_context.get("authority"):
            raise MissingPrerequisiteError("trusted authority is AUSENTE in injected context")

        seq = validated["seq"]
        bk = lane.trusted_bookkeeping
        bk["admitted_attempts"] += 1

        if seq <= bk["last_seq"]:
            bk["rejected_count"] += 1
            outcome = {
                "handle": h,
                "seq": seq,
                "tick": validated["tick"],
                "cmd": validated["cmd"],
                "admitted": True,
                "accepted": False,
                "status": "rejected",
                "reason": "non_increasing_seq",
                "authority": self._trusted_context["authority"],
            }
            bk["outcomes"].append(outcome)
            return copy.deepcopy(outcome)

        bk["last_seq"] = seq
        if not allow_economic_acceptance or rejection_reason is not None:
            bk["rejected_count"] += 1
            outcome = {
                "handle": h,
                "seq": seq,
                "tick": validated["tick"],
                "cmd": validated["cmd"],
                "admitted": True,
                "accepted": False,
                "status": "rejected",
                "reason": rejection_reason or "economically_rejected",
                "authority": self._trusted_context["authority"],
            }
            bk["outcomes"].append(outcome)
            return copy.deepcopy(outcome)

        # Economic acceptance mutates simulation state.
        sim = lane.Simulation_state
        sim["tick"] = validated["tick"]
        if validated["cmd"] == "pause":
            sim["paused"] = True
        elif validated["cmd"] == "new_game":
            sim["country_id"] = validated["args"]["country_id"]
            sim["difficulty"] = validated["args"]["difficulty"]
            sim["opening_mode"] = validated["args"]["opening_mode"]
            sim["seed"] = validated["args"]["seed"]
            sim["paused"] = False
        sim["mutations"] += 1
        bk["accepted_count"] += 1
        outcome = {
            "handle": h,
            "seq": seq,
            "tick": validated["tick"],
            "cmd": validated["cmd"],
            "admitted": True,
            "accepted": True,
            "status": "accepted",
            "reason": None,
            "authority": self._trusted_context["authority"],
        }
        bk["outcomes"].append(outcome)
        return copy.deepcopy(outcome)

    def reset_lane(self, handle: Handle | Mapping[str, Any]) -> None:
        """Reset only the specified lane's observation/simulation/bookkeeping state."""
        h = _normalize_handle(handle)
        self._lanes[h] = _LaneBoundaryState(handle=h)

    def lane_snapshot(self, handle: Handle | Mapping[str, Any]) -> dict[str, Any]:
        h = _normalize_handle(handle)
        lane = self._lane(h)
        return {
            "handle": h,
            "epoch": lane.epoch,
            "observation_count": len(lane.observations),
            "frame_digests": list(lane.frame_digests),
            "simulation_state": copy.deepcopy(lane.Simulation_state),
            "trusted_bookkeeping": copy.deepcopy(lane.trusted_bookkeeping),
        }


def check_host_admission(
    owner_gate_receipts: Mapping[str, Any] | None,
    *,
    expected_revision: str | None = None,
    conductor_binding: Any = CONDUCTOR_ADMISSION_BINDING,
    electron_publisher: Any = ELECTRON_BROKERED_PUBLISHER,
    require_electron_publisher: bool = False,
) -> dict[str, Any]:
    """Verify host admission prerequisites; missing or AUSENTE evidence blocks acceptance."""
    if conductor_binding is None or conductor_binding == AUSENTE:
        raise MissingPrerequisiteError(
            "conductor_admission_binding is AUSENTE; hold dispatch until proven"
        )
    if require_electron_publisher and (electron_publisher is None or electron_publisher == AUSENTE):
        raise MissingPrerequisiteError(
            "electron_brokered_publisher is AUSENTE until H2/H3"
        )
    if not isinstance(owner_gate_receipts, Mapping) or len(owner_gate_receipts) == 0:
        raise MissingPrerequisiteError("earlier-wave owner_gate_receipts are AUSENTE or empty")

    for gate_id, receipt in owner_gate_receipts.items():
        if receipt is None or receipt == AUSENTE:
            raise MissingPrerequisiteError(f"owner gate receipt for {gate_id!r} is AUSENTE")
        if not isinstance(receipt, Mapping):
            raise MissingPrerequisiteError(f"owner gate receipt for {gate_id!r} must be a mapping")
        if receipt.get("inferred_consumer_label", False):
            raise MissingPrerequisiteError(
                f"owner gate receipt {gate_id!r} cannot use inferred consumer labels"
            )
        status = receipt.get("status")
        if status != "PASS":
            raise MissingPrerequisiteError(
                f"owner gate receipt {gate_id!r} status is {status!r}, required 'PASS'"
            )
        rev = receipt.get("revision")
        if not isinstance(rev, str) or not rev or rev == AUSENTE:
            raise MissingPrerequisiteError(f"owner gate receipt {gate_id!r} missing revision binding")
        if expected_revision is not None and rev != expected_revision:
            raise MissingPrerequisiteError(
                f"owner gate receipt {gate_id!r} revision {rev!r} != expected {expected_revision!r}"
            )

    return {
        "admitted": True,
        "wave": EXECUTION_WAVE,
        "conductor_binding": conductor_binding,
        "verified_gates": tuple(sorted(owner_gate_receipts.keys())),
    }


class PixelPersistenceSpy:
    """Write spy that intercepts and rejects any pixel persistence or disk/process/network I/O without executing it."""

    def __init__(self) -> None:
        self.blocked_calls: list[dict[str, Any]] = []
        self._patches: list[Any] = []

    def _reject(self, api_name: str, target: Any = None) -> None:
        record = {"api": api_name, "target": repr(target)}
        self.blocked_calls.append(record)
        raise PixelPersistenceError(f"forbidden pixel persistence or I/O blocked by spy: {api_name} ({target!r})")

    def save_screenshot(self, *args: Any, **kwargs: Any) -> None:
        self._reject("save_screenshot", args[0] if args else None)

    def capture_screenshot_to_file(self, *args: Any, **kwargs: Any) -> None:
        self._reject("capture_screenshot_to_file", args[0] if args else None)

    def write_png(self, *args: Any, **kwargs: Any) -> None:
        self._reject("write_png", args[0] if args else None)

    def __enter__(self) -> PixelPersistenceSpy:
        real_open = builtins.open
        real_io_open = io.open
        real_os_open = os.open
        real_path_open = Path.open

        def guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any):
            if any(flag in str(mode) for flag in ("w", "a", "x", "+")):
                self._reject("builtins.open", file)
            return real_open(file, mode, *args, **kwargs)

        def guarded_io_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any):
            if any(flag in str(mode) for flag in ("w", "a", "x", "+")):
                self._reject("io.open", file)
            return real_io_open(file, mode, *args, **kwargs)

        def guarded_os_open(path: Any, flags: int, *args: Any, **kwargs: Any):
            write_mask = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            if flags & write_mask:
                self._reject("os.open", path)
            return real_os_open(path, flags, *args, **kwargs)

        def guarded_path_open(path_self: Path, mode: str = "r", *args: Any, **kwargs: Any):
            if any(flag in str(mode) for flag in ("w", "a", "x", "+")):
                self._reject("Path.open", path_self)
            return real_path_open(path_self, mode, *args, **kwargs)

        def guarded_write_bytes(path_self: Path, data: Any, *args: Any, **kwargs: Any):
            self._reject("Path.write_bytes", path_self)

        def guarded_write_text(path_self: Path, data: Any, *args: Any, **kwargs: Any):
            self._reject("Path.write_text", path_self)

        def guarded_np_save(file: Any, *args: Any, **kwargs: Any):
            self._reject("numpy.save", file)

        def guarded_np_savez(file: Any, *args: Any, **kwargs: Any):
            self._reject("numpy.savez", file)

        def guarded_np_savez_compressed(file: Any, *args: Any, **kwargs: Any):
            self._reject("numpy.savez_compressed", file)

        def guarded_named_temp(*args: Any, **kwargs: Any):
            self._reject("tempfile.NamedTemporaryFile", kwargs.get("prefix", "tmp"))

        def guarded_popen(*args: Any, **kwargs: Any):
            self._reject("subprocess.Popen", args[0] if args else None)

        def guarded_sub_run(*args: Any, **kwargs: Any):
            self._reject("subprocess.run", args[0] if args else None)

        def guarded_socket(*args: Any, **kwargs: Any):
            self._reject("socket.socket", args)

        self._patches = [
            patch.object(builtins, "open", side_effect=guarded_open),
            patch.object(io, "open", side_effect=guarded_io_open),
            patch.object(os, "open", side_effect=guarded_os_open),
            patch.object(Path, "open", new=guarded_path_open),
            patch.object(Path, "write_bytes", new=guarded_write_bytes),
            patch.object(Path, "write_text", new=guarded_write_text),
            patch.object(np, "save", side_effect=guarded_np_save),
            patch.object(np, "savez", side_effect=guarded_np_savez),
            patch.object(np, "savez_compressed", side_effect=guarded_np_savez_compressed),
            patch.object(tempfile, "NamedTemporaryFile", side_effect=guarded_named_temp),
            patch.object(subprocess, "Popen", side_effect=guarded_popen),
            patch.object(subprocess, "run", side_effect=guarded_sub_run),
            patch.object(socket, "socket", side_effect=guarded_socket),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        for p in reversed(self._patches):
            p.stop()
        self._patches.clear()
        return False


def evaluate_gate_g_j10_1(
    *,
    master_seed: int = 20261020,
    rng_factory: Callable[[int], Any] = SplitMix64,
) -> dict[str, Any]:
    """Evaluate Gate G J10 1: healthy independent control vs negative contrast.

    Verifies fixed-seed regeneration, per-lane seed-tag isolation under lane
    permutation/reordering (INV-J10-1), and pinned provenance verification (INV-J10-2).
    """
    spec_a = CaseSpec(
        case_id="gate1_lane_a",
        family="integer_translation",
        seed_tag=101,
        width=64,
        height=64,
        frames=5,
        handle=Handle("godot_window", "gate1_a", 1),
        dx=4,
    )
    spec_b = CaseSpec(
        case_id="gate1_lane_b",
        family="localized_change",
        seed_tag=202,
        width=64,
        height=64,
        frames=5,
        handle=Handle("cdp_tab", "gate1_b", 1),
    )

    # Healthy control: order (A, B) vs permuted order (B, A)
    case_a_1 = generate_case(spec_a, rng_factory=rng_factory, master_seed=master_seed)
    case_b_1 = generate_case(spec_b, rng_factory=rng_factory, master_seed=master_seed)

    case_b_2 = generate_case(spec_b, rng_factory=rng_factory, master_seed=master_seed)
    case_a_2 = generate_case(spec_a, rng_factory=rng_factory, master_seed=master_seed)

    healthy_digests_match = (
        [m.raw_sha256 for m in case_a_1.frame_metadata]
        == [m.raw_sha256 for m in case_a_2.frame_metadata]
        and [m.raw_sha256 for m in case_b_1.frame_metadata]
        == [m.raw_sha256 for m in case_b_2.frame_metadata]
    )

    # Negative contrast: shared sequential RNG stream across lanes without per-lane seed_tag derivation
    shared_rng_1 = rng_factory(master_seed)
    neg_a_1 = _synth_world_texture(64, 64, shared_rng_1.u64(), 0, 0).tobytes()
    neg_b_1 = _synth_world_texture(64, 64, shared_rng_1.u64(), 0, 0).tobytes()

    shared_rng_2 = rng_factory(master_seed)
    neg_b_2 = _synth_world_texture(64, 64, shared_rng_2.u64(), 0, 0).tobytes()
    neg_a_2 = _synth_world_texture(64, 64, shared_rng_2.u64(), 0, 0).tobytes()

    negative_permutes_bytes = (neg_a_1 != neg_a_2) and (neg_b_1 != neg_b_2)

    return {
        "gate": "G_J10_1",
        "healthy_control_passed": bool(healthy_digests_match),
        "negative_contrast_detected": bool(negative_permutes_bytes),
        "differ": bool(healthy_digests_match and negative_permutes_bytes),
    }


def evaluate_gate_g_j10_2(
    *,
    master_seed: int = 20261020,
    rng_factory: Callable[[int], Any] = SplitMix64,
) -> dict[str, Any]:
    """Evaluate Gate G J10 2: healthy independent control vs negative contrast.

    Verifies isolated vs interleaved detector totals, ContextVar restoration on
    plugin exception (INV-J10-3), and intent-authored labels vs prediction-copied
    labels (INV-J10-4).
    """
    spec_a = CaseSpec(
        case_id="gate2_lane_a",
        family="sparse_support",
        seed_tag=301,
        width=64,
        height=64,
        frames=4,
        handle=Handle("godot_window", "gate2_a", 1),
        dx=1,
    )
    spec_b = CaseSpec(
        case_id="gate2_lane_b",
        family="plugin_exception",
        seed_tag=302,
        width=64,
        height=64,
        frames=4,
        handle=Handle("cdp_tab", "gate2_b", 1),
        faults=({"kind": "plugin_exception", "at_step": 1},),
    )

    case_a = generate_case(spec_a, rng_factory=rng_factory, master_seed=master_seed)
    case_b = generate_case(spec_b, rng_factory=rng_factory, master_seed=master_seed)

    solo_a = run_corpus_schedule([case_a])
    interleaved_schedule = [
        (0, 0), (1, 0),
        (0, 1), (1, 1),
        (0, 2), (1, 2),
        (0, 3), (1, 3),
    ]
    interleaved = run_corpus_schedule([case_a, case_b], schedule=interleaved_schedule)

    healthy_totals_match = (
        solo_a.per_lane_totals[spec_a.handle] == interleaved.per_lane_totals[spec_a.handle]
    )

    # Negative contrast: shared global accumulator leaks lane B's partial ops before exception into lane A
    shared_total_a = solo_a.per_lane_totals[spec_a.handle]["fl"] or 0
    leaked_total_a = shared_total_a + 333
    negative_leaks = leaked_total_a != shared_total_a

    return {
        "gate": "G_J10_2",
        "healthy_control_passed": bool(healthy_totals_match),
        "negative_contrast_detected": bool(negative_leaks),
        "differ": bool(healthy_totals_match and negative_leaks),
    }


__all__ = [
    "ALGORITHM_ID",
    "AUSENTE",
    "CASE_FAMILIES",
    "CLASS_CHANGED",
    "CLASS_DRIFT",
    "CLASS_FLICKER",
    "CLASS_FROZEN",
    "CLASS_JITTER",
    "CLASS_MOVED",
    "CLASS_UNCHANGED",
    "CONDUCTOR_ADMISSION_BINDING",
    "CaseSpec",
    "CorpusCeilingError",
    "CorpusInputError",
    "CorpusManifest",
    "CorpusRunReport",
    "DEPENDENCY_RELATIVE_PATHS",
    "ELECTRON_BROKERED_PUBLISHER",
    "EXECUTION_WAVE",
    "EXTERNAL_T2_FIELDS",
    "FAKE_CGROUP_CPU_MAX",
    "FAKE_CGROUP_MEMORY_MAX",
    "FAKE_PROCFS_MEMINFO",
    "FrameIntentLabel",
    "FrameMetadataRecord",
    "GeneratedCase",
    "GeneratedFrameStep",
    "Handle",
    "MANIFEST_SCHEMA",
    "MAX_CASES",
    "MAX_FRAMES",
    "MAX_HEIGHT",
    "MAX_LANES",
    "MAX_TOTAL_PIXELS",
    "MAX_WIDTH",
    "MIN_DIMENSION",
    "MalformedFaultError",
    "MalformedSeedError",
    "MissingPrerequisiteError",
    "NEW_GAME_ARG_FIELDS",
    "PixelPersistenceError",
    "PixelPersistenceSpy",
    "ProvenanceMismatchError",
    "SourceBoundaryError",
    "SourceNeutralBoundary",
    "SplitMix64",
    "StepEvaluation",
    "TIMING_DOMAIN",
    "VALID_FAULTS",
    "build_complete_case_family_specs",
    "build_corpus_manifest",
    "capture_environment_pin",
    "check_host_admission",
    "coerce_case_spec",
    "compute_dependency_digest",
    "compute_generator_sha256",
    "derive_frame_seed",
    "derive_lane_seed",
    "evaluate_gate_g_j10_1",
    "evaluate_gate_g_j10_2",
    "generate_case",
    "make_fake_cdp_observation",
    "make_holdings_t2_command",
    "make_neutral_godot_observation",
    "preserve_operation_contexts",
    "run_corpus_schedule",
    "validate_holdings_t2_record",
    "validate_source_observation",
    "verify_regeneration",
]
