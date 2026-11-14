"""Holdings change statistics, generated corpus provenance, and regression contracts.

Implements ``describe_changes(baseline, candidate, protocol=None, ...)`` with:
- Complete-case sampling units (never pseudoreplicated frame/tile-frame units).
- Exact descriptive integer differences and reduced rational fractions.
- Explicit nulls (``None``) for unavailable evidence or missing operation counters.
- Confidence status ``AUSENTE`` when a reviewed protocol or confidence theorem is absent.
- Refusal of environment or generator mismatches and unequal batteries, with per-case
  exclusions and zero inferred improvement from unequal batteries.
- Negative controls for duplicated correlated frames and changed environment identity,
  neither of which may strengthen evidence.
- Source-neutral generated corpus provenance, manifest validation, and lane-isolated
  evaluation (``INV-J10-1`` through ``INV-J10-4``, Gate ``G J10 4``).
"""
from __future__ import annotations

import builtins
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import copy_context
import copy
from dataclasses import dataclass, field, fields, is_dataclass
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import platform as _platform_mod
import sys
import time
from typing import Any, Callable, Iterable, Mapping

import numpy as np

from eval.gui_synth.rng import SplitMix64

AUSENTE = "AUSENTE"
SCHEMA_VERSION = "holdings-change-statistics-v1"
MANIFEST_SCHEMA = "holdings-corpus-manifest-v1"
CONFIDENCE_STATUS_AUSENTE = AUSENTE
ELECTRON_BROKERED_PUBLISHER = AUSENTE
CONDUCTOR_ADMISSION_BINDING = AUSENTE
TIMING_SCOPE_VM = "VM"

SUPPORTED_HANDLE_KINDS = (
    "cdp_tab",
    "x11_window",
    "xvfb_screen",
    "capsule",
    "godot_window",
)

SUPPORTED_SOURCE_KINDS = (
    "cdp",
    "godot",
    "x11",
    "xvfb",
    "capsule",
    "neutral",
)

MANIFEST_REQUIRED_FIELDS = (
    "schema",
    "generator_sha256",
    "algorithm_id",
    "master_seed",
    "environment",
    "lanes",
    "cases",
    "frames",
)

ENVIRONMENT_REQUIRED_FIELDS = (
    "python",
    "numpy",
    "platform",
    "dependency_digest",
)

LANE_REQUIRED_FIELDS = (
    "handle",
    "epoch_plan",
    "case_ids",
)

CASE_REQUIRED_FIELDS = (
    "case_id",
    "seed_tag",
    "geometry",
    "faults",
    "labels",
)

FRAME_REQUIRED_FIELDS = (
    "case_id",
    "frame_seq",
    "pixel_decl_sha256",
    "raw_sha256",
)

T2_EXTERNAL_FIELDS = (
    "tick",
    "seq",
    "cmd",
    "args",
)

HOLDINGS_NEW_GAME_FIELDS = (
    "country_id",
    "difficulty",
    "opening_mode",
    "seed",
)

CLASS_NAMES = (
    "UNCHANGED",
    "JITTER",
    "CHANGED",
    "MOVED",
    "FLICKER",
    "FROZEN",
    "DRIFT",
)

CASE_FAMILIES = (
    "static_control",
    "localized_change",
    "integer_translation",
    "periodic_flicker_real_motion_veto",
    "sparse_support",
    "mixed_history",
    "skipped_frames",
    "resize_epoch",
    "caller_mutation",
    "plugin_exception",
    "cache_eviction",
)

MAX_FRAME_WIDTH = 4096
MAX_FRAME_HEIGHT = 4096
MAX_CASE_FRAMES = 512
MAX_TOTAL_CASE_BYTES = 64 * 1024 * 1024

_FORBIDDEN_IMAGE_SUFFIXES = frozenset({
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".ppm",
    ".pgm",
    ".pbm",
    ".webp",
    ".tiff",
    ".tif",
    ".gif",
    ".npy",
    ".npz",
    ".rgb",
    ".raw",
    ".tiles",
    ".swap",
})


class HoldingsStatisticsError(ValueError):
    """Base error for invalid Holdings statistics, provenance, or boundary inputs."""

    status = "REFUSED"

    def __init__(self, code: str, detail: str = ""):
        if not detail:
            detail = code
            code = "E_HOLDINGS_STATISTICS"
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class EnvironmentMismatchError(HoldingsStatisticsError):
    """Raised when pinned environment metadata is missing, malformed, or mismatched."""

    def __init__(self, detail: str = "environment mismatch"):
        super().__init__("E_ENVIRONMENT_MISMATCH", detail)


class GeneratorMismatchError(HoldingsStatisticsError):
    """Raised when generator digest, algorithm_id, or master_seed mismatches."""

    def __init__(self, detail: str = "generator mismatch"):
        super().__init__("E_GENERATOR_MISMATCH", detail)


class BatteryMismatchError(HoldingsStatisticsError):
    """Raised when baseline and candidate batteries are unequal under strict mode."""

    def __init__(self, detail: str = "unequal case battery"):
        super().__init__("E_BATTERY_MISMATCH", detail)


class ProtocolValidationError(HoldingsStatisticsError):
    """Raised when a statistical protocol object is malformed."""

    def __init__(self, detail: str = "invalid protocol"):
        super().__init__("E_PROTOCOL_INVALID", detail)


class PixelPersistenceError(HoldingsStatisticsError):
    """Raised by WriteSpy when any code attempts to persist frames or pixels."""

    def __init__(self, detail: str = "pixel persistence to disk/swap/logs is forbidden"):
        super().__init__("E_PIXEL_PERSISTENCE_FORBIDDEN", detail)


class SourceBoundaryError(HoldingsStatisticsError):
    """Raised when source-neutral boundary or T2 command invariants are violated."""

    def __init__(self, detail: str = "source boundary violation"):
        super().__init__("E_SOURCE_BOUNDARY", detail)


def _is_strict_int(value: Any) -> bool:
    return isinstance(value, (int, np.integer)) and not isinstance(value, (bool, np.bool_))


def _require_int(value: Any, name: str, *, min_value: int | None = None, error_cls=HoldingsStatisticsError) -> int:
    if not _is_strict_int(value):
        raise error_cls(f"{name} must be an integer (bool rejected), got {type(value).__name__}")
    ival = int(value)
    if min_value is not None and ival < min_value:
        raise error_cls(f"{name} must be >= {min_value}, got {ival}")
    return ival


def _require_nonempty_str(value: Any, name: str, *, error_cls=HoldingsStatisticsError) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error_cls(f"{name} must be a non-empty string")
    return value


def _require_sha256_hex(value: Any, name: str, *, error_cls=HoldingsStatisticsError) -> str:
    s = _require_nonempty_str(value, name, error_cls=error_cls)
    if len(s) != 64 or any(c not in "0123456789abcdef" for c in s):
        raise error_cls(f"{name} must be 64 lowercase hex characters")
    return s


class RationalValue(dict):
    """Exact rational representation supporting both dict, attribute, and Fraction access."""

    def __init__(self, numerator: int, denominator: int):
        num = _require_int(numerator, "numerator")
        den = _require_int(denominator, "denominator")
        if den <= 0:
            raise HoldingsStatisticsError("E_RATIONAL_DENOMINATOR", "denominator must be > 0")
        frac = Fraction(num, den)
        super().__init__(
            numerator=int(frac.numerator),
            denominator=int(frac.denominator),
            raw_numerator=int(num),
            raw_denominator=int(den),
            fraction=f"{frac.numerator}/{frac.denominator}",
            defined=True,
        )
        self._frac = frac

    @property
    def numerator(self) -> int:
        return self["numerator"]

    @property
    def denominator(self) -> int:
        return self["denominator"]

    @property
    def raw_numerator(self) -> int:
        return self["raw_numerator"]

    @property
    def raw_denominator(self) -> int:
        return self["raw_denominator"]

    @property
    def fraction(self) -> str:
        return self["fraction"]

    @property
    def defined(self) -> bool:
        return self["defined"]

    def as_fraction(self) -> Fraction:
        return self._frac

    def __str__(self) -> str:
        return self["fraction"]

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, RationalValue):
            return self._frac == other._frac
        if isinstance(other, Fraction):
            return self._frac == other
        if isinstance(other, str):
            return other in (self["fraction"], f"{self['raw_numerator']}/{self['raw_denominator']}")
        if isinstance(other, tuple) and len(other) == 2 and all(_is_strict_int(x) for x in other):
            if int(other[1]) == 0:
                return False
            return self._frac == Fraction(int(other[0]), int(other[1]))
        if isinstance(other, Mapping):
            if "numerator" in other and "denominator" in other:
                n = other.get("numerator")
                d = other.get("denominator")
                if _is_strict_int(n) and _is_strict_int(d) and int(d) > 0:
                    return self._frac == Fraction(int(n), int(d))
            return super().__eq__(other)
        return False


RationalFraction = RationalValue


def rational_fraction(numerator: int | None, denominator: int | None) -> RationalValue | None:
    """Return a RationalValue when defined and denominator > 0, else explicit None."""
    if numerator is None or denominator is None:
        return None
    num = _require_int(numerator, "numerator")
    den = _require_int(denominator, "denominator", min_value=0)
    if den == 0:
        return None
    return RationalValue(num, den)


class ChangeDescription(dict):
    """Immutable-Semantics dictionary report with attribute access for all keys."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


# ---------------------------------------------------------------------------
# Write Spy (Rejects pixel/frame persistence without executing real write)
# ---------------------------------------------------------------------------

class WriteSpy:
    """Context manager and spy that blocks any frame/pixel persistence to disk."""

    def __init__(self) -> None:
        self.blocked_attempts: list[dict[str, Any]] = []
        self._active = False
        self._patches: list[tuple[Any, str, Any]] = []

    def _record_and_reject(self, api: str, target: Any) -> None:
        self.blocked_attempts.append({"api": api, "target": str(target)})
        raise PixelPersistenceError(f"forbidden pixel persistence via {api} ({target})")

    def save_screenshot(self, path: Any, *_args: Any, **_kwargs: Any) -> None:
        """Fake screenshot-save API that immediately rejects pixel persistence."""
        self._record_and_reject("save_screenshot", path)

    def write_frame(self, path: Any, *_args: Any, **_kwargs: Any) -> None:
        """Fake frame-write API that immediately rejects pixel persistence."""
        self._record_and_reject("write_frame", path)

    def __enter__(self) -> WriteSpy:
        if self._active:
            return self
        self._active = True
        orig_open = builtins.open
        orig_np_save = np.save
        orig_np_savez = np.savez
        orig_np_savez_compressed = np.savez_compressed
        orig_write_bytes = Path.write_bytes
        orig_write_text = Path.write_text
        orig_os_open = os.open

        def _is_forbidden_path(file_arg: Any) -> bool:
            if isinstance(file_arg, (str, os.PathLike)):
                p = Path(file_arg)
                suf = p.suffix.lower()
                if suf in _FORBIDDEN_IMAGE_SUFFIXES:
                    return True
                parts = {part.lower() for part in p.parts}
                if "swap" in parts or "screenshots" in parts or "frames" in parts:
                    return True
                if len(p.parts) >= 2 and p.parts[-2] == "results" and p.parts[-3:-2] == ("eval",):
                    return True
                if "eval/results" in str(p).replace("\\", "/"):
                    return True
            return False

        def guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            if any(m in mode for m in ("w", "a", "x", "+")) and _is_forbidden_path(file):
                self._record_and_reject("builtins.open", file)
            return orig_open(file, mode, *args, **kwargs)

        def guarded_os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            if (flags & write_flags) and _is_forbidden_path(path):
                self._record_and_reject("os.open", path)
            return orig_os_open(path, flags, *args, **kwargs)

        def guarded_np_save(file: Any, *_args: Any, **_kwargs: Any) -> None:
            self._record_and_reject("numpy.save", file)

        def guarded_np_savez(file: Any, *_args: Any, **_kwargs: Any) -> None:
            self._record_and_reject("numpy.savez", file)

        def guarded_np_savez_compressed(file: Any, *_args: Any, **_kwargs: Any) -> None:
            self._record_and_reject("numpy.savez_compressed", file)

        def guarded_write_bytes(path_self: Path, data: Any) -> int:
            if _is_forbidden_path(path_self) or isinstance(data, np.ndarray):
                self._record_and_reject("Path.write_bytes", path_self)
            return orig_write_bytes(path_self, data)

        def guarded_write_text(path_self: Path, data: str, *args: Any, **kwargs: Any) -> int:
            if _is_forbidden_path(path_self):
                self._record_and_reject("Path.write_text", path_self)
            return orig_write_text(path_self, data, *args, **kwargs)

        self._patches = [
            (builtins, "open", orig_open),
            (os, "open", orig_os_open),
            (np, "save", orig_np_save),
            (np, "savez", orig_np_savez),
            (np, "savez_compressed", orig_np_savez_compressed),
            (Path, "write_bytes", orig_write_bytes),
            (Path, "write_text", orig_write_text),
        ]
        builtins.open = guarded_open
        os.open = guarded_os_open
        np.save = guarded_np_save
        np.savez = guarded_np_savez
        np.savez_compressed = guarded_np_savez_compressed
        Path.write_bytes = guarded_write_bytes
        Path.write_text = guarded_write_text
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        for obj, attr, orig in reversed(self._patches):
            setattr(obj, attr, orig)
        self._patches.clear()
        self._active = False
        return False


@contextmanager
def forbid_pixel_persistence():
    """Context manager installing a WriteSpy that rejects frame/pixel persistence."""
    spy = WriteSpy()
    with spy:
        yield spy


# ---------------------------------------------------------------------------
# Environment, Provenance, and Manifest Validation (INV-J10-1 .. INV-J10-4)
# ---------------------------------------------------------------------------

def default_generator_sha256() -> str:
    """Return a deterministic SHA-256 digest of the sealed RNG and this module."""
    h = hashlib.sha256()
    rng_path = Path(__file__).resolve().parents[1] / "gui_synth" / "rng.py"
    if rng_path.exists():
        h.update(rng_path.read_bytes())
    else:
        h.update(b"SplitMix64-v1")
    return h.hexdigest()


def build_environment_pin(
    *,
    python_version: str | None = None,
    numpy_version: str | None = None,
    platform_id: str | None = None,
    dependency_digest: str | None = None,
) -> dict[str, str]:
    """Create a canonical 4-field environment pin dict."""
    py_ver = python_version if python_version is not None else f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    np_ver = numpy_version if numpy_version is not None else str(np.__version__)
    plat = platform_id if platform_id is not None else f"{sys.platform}-{_platform_mod.machine()}"
    if dependency_digest is None:
        dep_src = f"python={py_ver};numpy={np_ver};platform={plat}".encode("utf-8")
        dep_dig = hashlib.sha256(dep_src).hexdigest()
    else:
        dep_dig = dependency_digest
    env = {
        "python": _require_nonempty_str(py_ver, "environment.python", error_cls=EnvironmentMismatchError),
        "numpy": _require_nonempty_str(np_ver, "environment.numpy", error_cls=EnvironmentMismatchError),
        "platform": _require_nonempty_str(plat, "environment.platform", error_cls=EnvironmentMismatchError),
        "dependency_digest": _require_nonempty_str(dep_dig, "environment.dependency_digest", error_cls=EnvironmentMismatchError),
    }
    return env


def validate_environment(env: Any) -> dict[str, str]:
    """Validate that an environment mapping pins python, numpy, platform, and dependency_digest."""
    if not isinstance(env, Mapping):
        raise EnvironmentMismatchError("environment must be a mapping")
    keys = set(env.keys())
    missing = set(ENVIRONMENT_REQUIRED_FIELDS) - keys
    extra = keys - set(ENVIRONMENT_REQUIRED_FIELDS)
    if missing:
        raise EnvironmentMismatchError(f"environment missing required fields: {sorted(missing)}")
    if extra:
        raise EnvironmentMismatchError(f"environment contains unexpected fields: {sorted(extra)}")
    out: dict[str, str] = {}
    for k in ENVIRONMENT_REQUIRED_FIELDS:
        val = env[k]
        if not isinstance(val, str) or not val.strip():
            raise EnvironmentMismatchError(f"environment.{k} must be a non-empty string")
        out[k] = val
    return out


def derive_lane_seed(master_seed: int, seed_tag: int, case_index: int = 0) -> int:
    """Derive an isolated 64-bit lane/case seed from master_seed and integer seed_tag (INV-J10-1)."""
    ms = _require_int(master_seed, "master_seed", min_value=0)
    st = _require_int(seed_tag, "seed_tag", min_value=0)
    ci = _require_int(case_index, "case_index", min_value=0)
    mix = SplitMix64(ms)
    s1 = mix.u64()
    s2 = SplitMix64(s1 ^ st).u64()
    s3 = SplitMix64(s2 ^ (ci * 0x9E3779B97F4A7C15)).u64()
    return int(s3)


def _normalize_handle_dict(handle: Any) -> dict[str, Any]:
    if isinstance(handle, Mapping):
        kind = handle.get("kind")
        cid = handle.get("canonical_id")
        gen = handle.get("generation", 0)
    else:
        kind = getattr(handle, "kind", None)
        cid = getattr(handle, "canonical_id", None)
        gen = getattr(handle, "generation", 0)
    if kind not in SUPPORTED_HANDLE_KINDS:
        raise HoldingsStatisticsError("E_HANDLE", f"unsupported handle kind: {kind!r}")
    cid_str = _require_nonempty_str(cid, "handle.canonical_id")
    gen_int = _require_int(gen, "handle.generation", min_value=0)
    return {"kind": kind, "canonical_id": cid_str, "generation": gen_int}


def canonical_json_bytes(obj: Any) -> bytes:
    """Serialize metadata canonically with sorted keys and integer-only numbers."""
    def _check(v: Any) -> Any:
        if v is None or isinstance(v, (str, bool)):
            return v
        if _is_strict_int(v):
            return int(v)
        if isinstance(v, float):
            raise HoldingsStatisticsError("E_FLOAT_FORBIDDEN", "floating-point numbers are forbidden in canonical manifest JSON")
        if isinstance(v, RationalValue):
            return {k: _check(val) for k, val in v.items()}
        if isinstance(v, Fraction):
            return {"numerator": int(v.numerator), "denominator": int(v.denominator), "fraction": f"{v.numerator}/{v.denominator}"}
        if isinstance(v, Mapping):
            out = {}
            for k, val in v.items():
                if not isinstance(k, str):
                    raise HoldingsStatisticsError("E_KEY_TYPE", "JSON keys must be strings")
                out[k] = _check(val)
            return out
        if isinstance(v, (list, tuple)):
            return [_check(x) for x in v]
        raise HoldingsStatisticsError("E_UNSERIALIZABLE", f"unsupported manifest type: {type(v).__name__}")

    cleaned = _check(obj)
    return json.dumps(cleaned, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def build_manifest(
    cases: Iterable[Mapping[str, Any]],
    *,
    environment: Mapping[str, Any],
    generator_sha256: str | None = None,
    algorithm_id: str = "j10-holdings-synth-v1",
    master_seed: int = 0,
    schema: str = MANIFEST_SCHEMA,
    lanes: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build and seal a source-neutral corpus manifest (INV-J10-1, INV-J10-2, INV-J10-4)."""
    env = validate_environment(environment)
    gen_sha = _require_sha256_hex(generator_sha256 or default_generator_sha256(), "generator_sha256", error_cls=GeneratorMismatchError)
    alg_id = _require_nonempty_str(algorithm_id, "algorithm_id", error_cls=GeneratorMismatchError)
    m_seed = _require_int(master_seed, "master_seed", min_value=0, error_cls=GeneratorMismatchError)
    schema_str = _require_nonempty_str(schema, "schema")

    case_list = list(cases)
    if not case_list:
        raise HoldingsStatisticsError("E_EMPTY_CASES", "cases must not be empty")

    seen_case_ids: set[str] = set()
    seen_frames: set[tuple[str, int]] = set()
    canonical_cases: list[dict[str, Any]] = []
    canonical_frames: list[dict[str, Any]] = []
    lane_map: dict[tuple[str, str, int], dict[str, Any]] = {}

    for raw_case in case_list:
        if not isinstance(raw_case, Mapping):
            raise HoldingsStatisticsError("E_CASE_TYPE", "each case must be a mapping")
        if raw_case.get("labels_source") == "prediction" or raw_case.get("from_predictions") is True:
            raise HoldingsStatisticsError("E_PREDICTION_LABELS", "INV-J10-4 forbids expected labels derived from predictions")
        cid = _require_nonempty_str(raw_case.get("case_id"), "case.case_id")
        if cid in seen_case_ids:
            raise HoldingsStatisticsError("E_DUPLICATE_CASE", f"duplicate case_id: {cid}")
        seen_case_ids.add(cid)
        seed_tag = _require_int(raw_case.get("seed_tag", 0), "case.seed_tag", min_value=0)
        geom = raw_case.get("geometry")
        if not isinstance(geom, Mapping):
            raise HoldingsStatisticsError("E_GEOMETRY", f"case {cid} missing geometry mapping")
        width = _require_int(geom.get("width", geom.get("w")), "geometry.width", min_value=16)
        height = _require_int(geom.get("height", geom.get("h")), "geometry.height", min_value=16)
        tile_size = _require_int(geom.get("tile_size", geom.get("s", 16)), "geometry.tile_size", min_value=8)
        faults = list(raw_case.get("faults", ()))
        for f_item in faults:
            if not isinstance(f_item, str):
                raise HoldingsStatisticsError("E_FAULT_SPEC", "each fault must be a string")

        labels = raw_case.get("labels", raw_case.get("expected_labels"))
        if labels is None:
            raise HoldingsStatisticsError("E_MISSING_LABELS", f"case {cid} missing independent expected labels")
        if isinstance(labels, np.ndarray):
            if labels.dtype.kind not in ("i", "u") or labels.dtype == np.bool_:
                raise HoldingsStatisticsError("E_LABELS_DTYPE", "labels array must be integer")
            label_summary = {
                "shape": [int(x) for x in labels.shape],
                "sha256": hashlib.sha256(np.ascontiguousarray(labels, dtype=np.int64).tobytes()).hexdigest(),
                "source": "generation_intent",
            }
        elif isinstance(labels, Mapping):
            if labels.get("source") == "prediction":
                raise HoldingsStatisticsError("E_PREDICTION_LABELS", "INV-J10-4 forbids prediction-sourced labels")
            label_summary = dict(labels)
            label_summary.setdefault("source", "generation_intent")
        elif isinstance(labels, (list, tuple)):
            arr = np.asarray(labels, dtype=np.int64)
            label_summary = {
                "shape": [int(x) for x in arr.shape],
                "sha256": hashlib.sha256(np.ascontiguousarray(arr, dtype=np.int64).tobytes()).hexdigest(),
                "source": "generation_intent",
            }
        else:
            raise HoldingsStatisticsError("E_LABELS_TYPE", f"unsupported labels type in case {cid}")

        handle_dict = _normalize_handle_dict(
            raw_case.get("handle", {"kind": "godot_window", "canonical_id": f"lane_{cid}", "generation": 1})
        )
        epoch = _require_int(raw_case.get("epoch", 1), "case.epoch", min_value=0)
        h_key = (handle_dict["kind"], handle_dict["canonical_id"], handle_dict["generation"])
        if h_key not in lane_map:
            lane_map[h_key] = {
                "handle": handle_dict,
                "epoch_plan": [epoch],
                "case_ids": [cid],
            }
        else:
            if epoch not in lane_map[h_key]["epoch_plan"]:
                lane_map[h_key]["epoch_plan"].append(epoch)
            lane_map[h_key]["case_ids"].append(cid)

        frames_in = raw_case.get("frame_metadata", raw_case.get("frames", ()))
        for idx, fr in enumerate(frames_in):
            if isinstance(fr, np.ndarray):
                raw_sha = hashlib.sha256(np.ascontiguousarray(fr, dtype=np.uint8).tobytes()).hexdigest()
                decl_sha = hashlib.sha256(f"{cid}:{idx}:{fr.shape}".encode("utf-8")).hexdigest()
                f_entry: dict[str, Any] = {
                    "case_id": cid,
                    "frame_seq": idx,
                    "pixel_decl_sha256": decl_sha,
                    "raw_sha256": raw_sha,
                    "normalized_sha256": raw_sha,
                }
            elif isinstance(fr, Mapping):
                f_cid = _require_nonempty_str(fr.get("case_id", cid), "frame.case_id")
                if f_cid != cid:
                    raise HoldingsStatisticsError("E_FRAME_CASE_MISMATCH", f"frame case_id {f_cid} != {cid}")
                f_seq = _require_int(fr.get("frame_seq", idx), "frame.frame_seq", min_value=0)
                if (cid, f_seq) in seen_frames:
                    raise HoldingsStatisticsError("E_DUPLICATE_FRAME", f"duplicate (case_id, frame_seq): {(cid, f_seq)}")
                seen_frames.add((cid, f_seq))
                p_sha = _require_sha256_hex(fr.get("pixel_decl_sha256"), "frame.pixel_decl_sha256")
                r_sha = _require_sha256_hex(fr.get("raw_sha256"), "frame.raw_sha256")
                n_sha = fr.get("normalized_sha256")
                if n_sha is not None:
                    n_sha = _require_sha256_hex(n_sha, "frame.normalized_sha256")
                f_entry = {
                    "case_id": cid,
                    "frame_seq": f_seq,
                    "pixel_decl_sha256": p_sha,
                    "raw_sha256": r_sha,
                    "normalized_sha256": n_sha,
                }
            else:
                raise HoldingsStatisticsError("E_FRAME_TYPE", "invalid frame metadata entry")
            canonical_frames.append(f_entry)

        canonical_cases.append({
            "case_id": cid,
            "seed_tag": seed_tag,
            "geometry": {"width": width, "height": height, "tile_size": tile_size},
            "faults": faults,
            "labels": label_summary,
        })

    canonical_cases.sort(key=lambda c: c["case_id"])
    canonical_frames.sort(key=lambda f: (f["case_id"], f["frame_seq"]))

    if lanes is not None:
        canonical_lanes = []
        for ln in lanes:
            if not isinstance(ln, Mapping):
                raise HoldingsStatisticsError("E_LANE_TYPE", "lane must be a mapping")
            h_d = _normalize_handle_dict(ln.get("handle"))
            ep_plan = [_require_int(e, "lane.epoch_plan", min_value=0) for e in ln.get("epoch_plan", (1,))]
            c_ids = sorted(_require_nonempty_str(c, "lane.case_id") for c in ln.get("case_ids", ()))
            canonical_lanes.append({
                "handle": h_d,
                "epoch_plan": ep_plan,
                "case_ids": c_ids,
            })
    else:
        canonical_lanes = []
        for h_key in sorted(lane_map.keys()):
            ln = lane_map[h_key]
            canonical_lanes.append({
                "handle": ln["handle"],
                "epoch_plan": sorted(ln["epoch_plan"]),
                "case_ids": sorted(ln["case_ids"]),
            })
    canonical_lanes.sort(
        key=lambda l: (
            l["handle"]["kind"],
            l["handle"]["canonical_id"],
            l["handle"]["generation"],
        )
    )

    manifest_body = {
        "schema": schema_str,
        "generator_sha256": gen_sha,
        "algorithm_id": alg_id,
        "master_seed": m_seed,
        "environment": env,
        "lanes": canonical_lanes,
        "cases": canonical_cases,
        "frames": canonical_frames,
    }
    seal = hashlib.sha256(canonical_json_bytes(manifest_body)).hexdigest()
    manifest_body["manifest_sha256"] = seal
    return manifest_body


def validate_manifest(
    manifest: Mapping[str, Any],
    *,
    expected_environment: Mapping[str, Any] | None = None,
    expected_generator_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate all required fields, seals, and provenance bindings of a corpus manifest."""
    if not isinstance(manifest, Mapping):
        raise HoldingsStatisticsError("E_MANIFEST_TYPE", "manifest must be a mapping")
    missing = set(MANIFEST_REQUIRED_FIELDS) - set(manifest.keys())
    if missing:
        raise HoldingsStatisticsError("E_MANIFEST_FIELDS", f"manifest missing required fields: {sorted(missing)}")

    env = validate_environment(manifest["environment"])
    if expected_environment is not None:
        exp_env = validate_environment(expected_environment)
        if env != exp_env:
            raise EnvironmentMismatchError("manifest environment does not match expected environment")

    gen_sha = _require_sha256_hex(manifest["generator_sha256"], "manifest.generator_sha256", error_cls=GeneratorMismatchError)
    if expected_generator_sha256 is not None:
        exp_sha = _require_sha256_hex(expected_generator_sha256, "expected_generator_sha256", error_cls=GeneratorMismatchError)
        if gen_sha != exp_sha:
            raise GeneratorMismatchError("manifest generator_sha256 does not match expected digest")

    _require_nonempty_str(manifest["algorithm_id"], "manifest.algorithm_id", error_cls=GeneratorMismatchError)
    _require_int(manifest["master_seed"], "manifest.master_seed", min_value=0, error_cls=GeneratorMismatchError)

    lanes = manifest["lanes"]
    cases = manifest["cases"]
    frames = manifest["frames"]
    if not isinstance(lanes, (list, tuple)) or not isinstance(cases, (list, tuple)) or not isinstance(frames, (list, tuple)):
        raise HoldingsStatisticsError("E_MANIFEST_COLLECTIONS", "lanes, cases, and frames must be sequences")

    for ln in lanes:
        if not isinstance(ln, Mapping) or set(LANE_REQUIRED_FIELDS) - set(ln.keys()):
            raise HoldingsStatisticsError("E_LANE_FIELDS", "lane missing required fields (handle, epoch_plan, case_ids)")
        _normalize_handle_dict(ln["handle"])

    seen_cases: set[str] = set()
    for c in cases:
        if not isinstance(c, Mapping) or set(CASE_REQUIRED_FIELDS) - set(c.keys()):
            raise HoldingsStatisticsError("E_CASE_FIELDS", "case missing required fields")
        cid = _require_nonempty_str(c["case_id"], "case.case_id")
        if cid in seen_cases:
            raise HoldingsStatisticsError("E_DUPLICATE_CASE", f"duplicate case_id in manifest: {cid}")
        seen_cases.add(cid)
        _require_int(c["seed_tag"], "case.seed_tag", min_value=0)
        if isinstance(c["labels"], Mapping) and c["labels"].get("source") == "prediction":
            raise HoldingsStatisticsError("E_PREDICTION_LABELS", "INV-J10-4 forbids prediction labels")

    seen_frame_keys: set[tuple[str, int]] = set()
    for f in frames:
        if not isinstance(f, Mapping) or set(FRAME_REQUIRED_FIELDS) - set(f.keys()):
            raise HoldingsStatisticsError("E_FRAME_FIELDS", "frame missing required fields")
        fcid = _require_nonempty_str(f["case_id"], "frame.case_id")
        if fcid not in seen_cases:
            raise HoldingsStatisticsError("E_ORPHAN_FRAME", f"frame references unknown case_id {fcid}")
        fseq = _require_int(f["frame_seq"], "frame.frame_seq", min_value=0)
        if (fcid, fseq) in seen_frame_keys:
            raise HoldingsStatisticsError("E_DUPLICATE_FRAME", f"duplicate frame (case_id, frame_seq): {(fcid, fseq)}")
        seen_frame_keys.add((fcid, fseq))
        _require_sha256_hex(f["pixel_decl_sha256"], "frame.pixel_decl_sha256")
        _require_sha256_hex(f["raw_sha256"], "frame.raw_sha256")
        if f.get("normalized_sha256") is not None:
            _require_sha256_hex(f["normalized_sha256"], "frame.normalized_sha256")

    return dict(manifest)


# ---------------------------------------------------------------------------
# Volatile RAM Case Generation & Lane-Isolated Detector Evaluation
# ---------------------------------------------------------------------------

def _make_s2_baseline_tiles(rgb: np.ndarray, cls_override: np.ndarray | int = 0) -> np.ndarray:
    """Create a read-only S2 TILE_DTYPE array matching Grid(W, H) in RAM."""
    from visioncore import desc
    from visioncore.grid import Grid

    h, w, _ = rgb.shape
    grid = Grid(w, h)
    padded = np.array(grid.pad(rgb), copy=True, order="C")
    tiles, _ = desc.describe_padded(padded, grid, lazy_xh=False)
    out = tiles.copy()
    out["cls"] = cls_override
    out["mv_dx"] = 0
    out["mv_dy"] = 0
    out.flags.writeable = False
    return out


def generate_case(
    spec: Mapping[str, Any],
    *,
    rng_factory: Callable[[int], Any] = SplitMix64,
    master_seed: int = 0,
    environment: Mapping[str, Any] | None = None,
    generator_sha256: str | None = None,
    expected_environment: Mapping[str, Any] | None = None,
    expected_generator_sha256: str | None = None,
    expected_master_seed: int | None = None,
) -> dict[str, Any]:
    """Generate a deterministic volatile uint8 case in RAM with independent intent labels.

    Satisfies INV-J10-1 (per-lane seed isolation), INV-J10-2 (pinned seed, generator,
    and environment check before regeneration), and INV-J10-4 (expected labels from
    generation intent, never detector predictions).
    """
    if not isinstance(spec, Mapping):
        raise HoldingsStatisticsError("E_SPEC_TYPE", "spec must be a mapping")
    spec_copy = copy.deepcopy(dict(spec))

    env = validate_environment(environment or spec_copy.get("environment") or build_environment_pin())
    if expected_environment is not None:
        if env != validate_environment(expected_environment):
            raise EnvironmentMismatchError("regeneration refused: environment mismatch (INV-J10-2)")

    gen_sha = _require_sha256_hex(
        generator_sha256 or spec_copy.get("generator_sha256") or default_generator_sha256(),
        "generator_sha256",
        error_cls=GeneratorMismatchError,
    )
    if expected_generator_sha256 is not None:
        if gen_sha != _require_sha256_hex(expected_generator_sha256, "expected_generator_sha256", error_cls=GeneratorMismatchError):
            raise GeneratorMismatchError("regeneration refused: generator_sha256 mismatch (INV-J10-2)")

    m_seed = _require_int(
        spec_copy.get("master_seed", master_seed),
        "master_seed",
        min_value=0,
        error_cls=GeneratorMismatchError,
    )
    if expected_master_seed is not None:
        if m_seed != _require_int(expected_master_seed, "expected_master_seed", min_value=0, error_cls=GeneratorMismatchError):
            raise GeneratorMismatchError("regeneration refused: master_seed mismatch (INV-J10-2)")

    case_id = _require_nonempty_str(spec_copy.get("case_id", "case_0"), "case_id")
    seed_tag = _require_int(spec_copy.get("seed_tag", 1), "seed_tag", min_value=0)
    family = _require_nonempty_str(spec_copy.get("family", "static_control"), "family")
    if family not in CASE_FAMILIES:
        raise HoldingsStatisticsError("E_CASE_FAMILY", f"unsupported case family: {family!r}")

    geom = spec_copy.get("geometry", {"width": 64, "height": 64, "tile_size": 16})
    if not isinstance(geom, Mapping):
        raise HoldingsStatisticsError("E_GEOMETRY", "geometry must be a mapping")
    width = _require_int(geom.get("width", 64), "geometry.width", min_value=16)
    height = _require_int(geom.get("height", 64), "geometry.height", min_value=16)
    tile_size = _require_int(geom.get("tile_size", 16), "geometry.tile_size", min_value=16)
    n_frames = _require_int(spec_copy.get("n_frames", 6), "n_frames", min_value=1)

    if width > MAX_FRAME_WIDTH or height > MAX_FRAME_HEIGHT or n_frames > MAX_CASE_FRAMES:
        raise HoldingsStatisticsError("E_GEOMETRY_CEILING", "geometry or frame count exceeds finite allocation ceiling")
    if width * height * 3 * n_frames > MAX_TOTAL_CASE_BYTES:
        raise HoldingsStatisticsError("E_BYTE_CEILING", "case byte footprint exceeds finite RAM allocation ceiling")

    faults_raw = spec_copy.get("faults", ())
    if not isinstance(faults_raw, (list, tuple)):
        raise HoldingsStatisticsError("E_FAULTS", "faults must be a list or tuple of fault names")
    allowed_faults = {"skip_frame", "resize_epoch", "plugin_exception", "caller_mutation", "cache_eviction", "reset_lane"}
    faults = []
    for f_name in faults_raw:
        if not isinstance(f_name, str) or f_name not in allowed_faults:
            raise HoldingsStatisticsError("E_FAULT_UNKNOWN", f"invalid fault specification: {f_name!r}")
        faults.append(f_name)

    handle_dict = _normalize_handle_dict(
        spec_copy.get("handle", {"kind": "godot_window", "canonical_id": f"win_{case_id}", "generation": 1})
    )
    epoch = _require_int(spec_copy.get("epoch", 1), "epoch", min_value=0)

    derived_seed = derive_lane_seed(m_seed, seed_tag, 0)
    rng = rng_factory(derived_seed)
    gw = (width + tile_size - 1) // tile_size
    gh = (height + tile_size - 1) // tile_size
    n_tiles = gw * gh

    # Base textured canvas generated deterministically from SplitMix64
    base_canvas = np.zeros((height, width, 3), dtype=np.uint8)
    for y in range(height):
        row_seed = rng.u64()
        for x in range(width):
            v = ((row_seed >> ((x % 8) * 8)) & 0xFF) ^ ((x * 17 + y * 31) & 0xFF)
            base_canvas[y, x, 0] = v
            base_canvas[y, x, 1] = (v + 43) & 0xFF
            base_canvas[y, x, 2] = (v + 97) & 0xFF

    frames_rgb: list[np.ndarray] = []
    baseline_tiles_list: list[np.ndarray] = []
    expected_labels = np.zeros((n_frames, n_tiles), dtype=np.int64)
    frame_seqs: list[int] = []
    epochs: list[int] = []
    frame_meta: list[dict[str, Any]] = []

    for f_idx in range(n_frames):
        cur_rgb = base_canvas.copy()
        base_cls = np.zeros(n_tiles, dtype=np.uint8)
        intent_cls = np.zeros(n_tiles, dtype=np.int64)
        cur_seq = f_idx + 1
        cur_epoch = epoch

        if family == "static_control":
            pass
        elif family == "localized_change":
            if f_idx >= 1:
                cur_rgb[0:tile_size, 0:tile_size, :] = np.uint8((f_idx * 53) & 0xFF)
                base_cls[0] = 2
                intent_cls[0] = 2
        elif family == "integer_translation":
            dx = 4 * f_idx
            cur_rgb = np.roll(base_canvas, shift=dx, axis=1)
            if f_idx >= 1:
                base_cls[:] = 2
                intent_cls[:] = 3
        elif family == "periodic_flicker_real_motion_veto":
            # Periodic 2-state toggle on tile 0, overridden by global shift on last frame
            if f_idx < n_frames - 1:
                if f_idx % 2 == 1:
                    cur_rgb[0:tile_size, 0:tile_size, :] ^= np.uint8(0x7F)
                if f_idx >= 1:
                    base_cls[0] = 4 if f_idx >= 3 else 2
                    intent_cls[0] = 4 if f_idx >= 3 else 2
            else:
                cur_rgb = np.roll(base_canvas, shift=4, axis=1)
                base_cls[:] = 4
                intent_cls[:] = 2
        elif family == "sparse_support":
            if f_idx >= 1 and n_tiles >= 2:
                cur_rgb[0:2, 0:2, :] ^= np.uint8(0xAA)
                base_cls[0] = 2
                intent_cls[0] = 2
        elif family == "mixed_history":
            if f_idx == 1:
                cur_rgb[0:tile_size, 0:tile_size, :] ^= np.uint8(0x33)
                base_cls[0] = 2
                intent_cls[0] = 2
            elif f_idx >= 2:
                intent_cls[:] = 0
        elif family == "skipped_frames" or "skip_frame" in faults:
            if f_idx >= 2:
                cur_seq = f_idx + 2  # Intentional sequence gap
        elif family == "resize_epoch" or "resize_epoch" in faults:
            if f_idx >= n_frames // 2:
                cur_epoch = epoch + 1
        elif family == "caller_mutation":
            if f_idx == 1:
                cur_rgb[0:tile_size, 0:tile_size, :] ^= np.uint8(0x55)
                base_cls[0] = 2
                intent_cls[0] = 2
        elif family == "plugin_exception":
            pass
        elif family == "cache_eviction":
            cur_rgb[0:tile_size, 0:tile_size, 0] = np.uint8((f_idx * 19 + 7) & 0xFF)
            if f_idx >= 1:
                base_cls[0] = 2
                intent_cls[0] = 2

        cur_rgb = np.ascontiguousarray(cur_rgb, dtype=np.uint8).copy()
        cur_rgb.flags.writeable = False
        s2_tiles = _make_s2_baseline_tiles(cur_rgb, base_cls)

        raw_sha = hashlib.sha256(cur_rgb.tobytes(order="C")).hexdigest()
        decl_sha = hashlib.sha256(
            f"{case_id}:{family}:{cur_epoch}:{cur_seq}:{width}x{height}:{intent_cls.tobytes().hex()}".encode("utf-8")
        ).hexdigest()

        frames_rgb.append(cur_rgb)
        baseline_tiles_list.append(s2_tiles)
        expected_labels[f_idx, :] = intent_cls
        frame_seqs.append(cur_seq)
        epochs.append(cur_epoch)
        frame_meta.append({
            "case_id": case_id,
            "frame_seq": cur_seq,
            "pixel_decl_sha256": decl_sha,
            "raw_sha256": raw_sha,
            "normalized_sha256": raw_sha,
        })

    expected_labels.flags.writeable = False
    return {
        "case_id": case_id,
        "seed_tag": seed_tag,
        "derived_seed": derived_seed,
        "family": family,
        "handle": handle_dict,
        "epoch": epoch,
        "epochs": tuple(epochs),
        "frame_seqs": tuple(frame_seqs),
        "geometry": {"width": width, "height": height, "tile_size": tile_size},
        "faults": tuple(faults),
        "frames_rgb": tuple(frames_rgb),
        "baseline_tiles": tuple(baseline_tiles_list),
        "labels": expected_labels,
        "labels_source": "generation_intent",
        "frame_metadata": tuple(frame_meta),
        "environment": env,
        "generator_sha256": gen_sha,
        "master_seed": m_seed,
    }


def evaluate_lanes(
    cases: Iterable[Mapping[str, Any]],
    *,
    detector: Any = None,
    gm: Any = None,
    fl: Any = None,
    veto: Any = None,
    environment: Mapping[str, Any] | None = None,
    generator_sha256: str | None = None,
    master_seed: int = 0,
    algorithm_id: str = "j10-holdings-synth-v1",
) -> dict[str, Any]:
    """Run PerWindowDetector over generated volatile cases with per-lane isolation.

    Enforces:
    - INV-J10-1: RNG, history, and cache isolated per lane under reordering.
    - INV-J10-2: Regeneration/evaluation bound to pinned seed, generator, and environment.
    - INV-J10-3: Preserves caller GM and FL ContextVar operation contexts and restores on exceptions.
    - INV-J10-4: Compares detector claims against independent intent labels; missing counters stay None.
    """
    from eval.v2x.per_window_detector import Handle, PerWindowDetector

    env = validate_environment(environment or build_environment_pin())
    gen_sha = _require_sha256_hex(generator_sha256 or default_generator_sha256(), "generator_sha256")
    det = detector if detector is not None else PerWindowDetector(gm=gm, fl=fl, veto=veto)

    case_list = list(cases)
    case_results: list[dict[str, Any]] = []
    manifest_cases: list[dict[str, Any]] = []

    for c in case_list:
        cid = _require_nonempty_str(c["case_id"], "case.case_id")
        h_info = _normalize_handle_dict(c["handle"])
        handle = Handle(h_info["kind"], h_info["canonical_id"], h_info["generation"])
        frames_rgb = c["frames_rgb"]
        base_tiles = c["baseline_tiles"]
        expected = np.asarray(c["labels"], dtype=np.int64)
        f_seqs = c.get("frame_seqs", tuple(range(1, len(frames_rgb) + 1)))
        f_epochs = c.get("epochs", tuple(c.get("epoch", 1) for _ in frames_rgb))
        faults = tuple(c.get("faults", ()))

        pred_classes: list[np.ndarray] = []
        gm_ops_total: int | None = 0
        fl_ops_total: int | None = 0
        settled_final = False
        status = "OK"
        error_msg: str | None = None

        for idx, (rgb, tiles, seq, ep) in enumerate(zip(frames_rgb, base_tiles, f_seqs, f_epochs)):
            if "reset_lane" in faults and idx == len(frames_rgb) // 2:
                det.reset_window(handle)
            try:
                # Copy context so any plugin exception inside detector never corrupts outer ContextVars (INV-J10-3)
                res = copy_context().run(
                    lambda: det.update(handle, tiles, rgb, epoch=int(ep), frame_seq=int(seq))
                )
            except BaseException as exc:
                status = "INCOMPLETE"
                error_msg = f"{type(exc).__name__}: {exc}"
                break

            pred_classes.append(np.asarray(res.claims["cls"], dtype=np.int64).copy())
            settled_final = bool(res.settled)
            gm_step = res.ops.get("gm") if isinstance(res.ops, Mapping) else None
            fl_step = res.ops.get("fl") if isinstance(res.ops, Mapping) else None
            if gm_ops_total is not None and _is_strict_int(gm_step):
                gm_ops_total += int(gm_step)
            else:
                gm_ops_total = None
            if fl_ops_total is not None and _is_strict_int(fl_step):
                fl_ops_total += int(fl_step)
            else:
                fl_ops_total = None

        if status == "OK" and len(pred_classes) == len(frames_rgb):
            pred_arr = np.stack(pred_classes, axis=0)
            if pred_arr.shape != expected.shape:
                raise HoldingsStatisticsError("E_SHAPE_MISMATCH", f"predicted shape {pred_arr.shape} != expected {expected.shape}")
            correct = int(np.count_nonzero(pred_arr == expected))
            total = int(expected.size)
            errors = total - correct
            mv_mask = expected == 3
            mv_total = int(np.count_nonzero(mv_mask))
            mv_correct = int(np.count_nonzero((pred_arr == 3) & mv_mask))
            fl_mask = expected == 4
            fl_total = int(np.count_nonzero(fl_mask))
            fl_correct = int(np.count_nonzero((pred_arr == 4) & fl_mask))
            ch_mask = expected == 2
            ch_total = int(np.count_nonzero(ch_mask))
            ch_correct = int(np.count_nonzero((pred_arr == 2) & ch_mask))
            static_mask = (expected == 0) | (expected == 1)
            fp_total = int(np.count_nonzero(static_mask))
            fp_count = int(np.count_nonzero((pred_arr >= 2) & static_mask))
            complete = True
        else:
            correct = 0
            total = int(expected.size)
            errors = total
            mv_total = mv_correct = 0
            fl_total = fl_correct = 0
            ch_total = ch_correct = 0
            fp_total = fp_count = 0
            complete = False
            gm_ops_total = None
            fl_ops_total = None

        case_results.append({
            "case_id": cid,
            "handle": h_info,
            "epoch": int(f_epochs[-1]) if f_epochs else 1,
            "status": status,
            "complete": complete,
            "error": error_msg,
            "faults": list(faults),
            "settled": 1 if (complete and settled_final) else (0 if complete else None),
            "frames": len(pred_classes),
            "expected_frames": len(frames_rgb),
            "metrics": {
                "correct": correct if complete else None,
                "total": total if complete else None,
                "errors": errors if complete else None,
                "fp": fp_count if complete else None,
                "fp_total": fp_total if complete else None,
                "moved_correct": mv_correct if complete else None,
                "moved_total": mv_total if complete else None,
                "flicker_correct": fl_correct if complete else None,
                "flicker_total": fl_total if complete else None,
                "changed_correct": ch_correct if complete else None,
                "changed_total": ch_total if complete else None,
            },
            "ops": {
                "gm": gm_ops_total,
                "fl": fl_ops_total,
            },
            "frame_metadata": list(c.get("frame_metadata", ())),
        })
        manifest_cases.append(c)

    manifest = build_manifest(
        manifest_cases,
        environment=env,
        generator_sha256=gen_sha,
        algorithm_id=algorithm_id,
        master_seed=master_seed,
    )
    return {
        "schema": SCHEMA_VERSION,
        "environment": env,
        "generator_sha256": gen_sha,
        "algorithm_id": algorithm_id,
        "master_seed": master_seed,
        "manifest": manifest,
        "cases": case_results,
        "timing_scope": TIMING_SCOPE_VM,
    }


# ---------------------------------------------------------------------------
# Source-Neutral Boundary & Holdings T2 Command / Observation Contracts
# ---------------------------------------------------------------------------

def make_holdings_command(cmd: str, *, tick: int = 0, seq: int = 0, **kwargs: Any) -> dict[str, Any]:
    """Construct a validated external T2 Holdings command inline per COMMANDS.md and CONTRACTS.md.

    External T2 fields are strictly (tick, seq, cmd, args).
    - ``pause`` has empty ``args`` (``{}``).
    - ``new_game`` has ``country_id``, ``difficulty``, ``opening_mode``, and ``seed``.
    """
    t = _require_int(tick, "tick", min_value=0, error_cls=SourceBoundaryError)
    s = _require_int(seq, "seq", min_value=0, error_cls=SourceBoundaryError)
    cmd_str = _require_nonempty_str(cmd, "cmd", error_cls=SourceBoundaryError)
    if cmd_str == "pause":
        if kwargs:
            raise SourceBoundaryError(f"pause command requires empty args, got {sorted(kwargs.keys())}")
        args: dict[str, Any] = {}
    elif cmd_str == "new_game":
        if set(kwargs.keys()) != set(HOLDINGS_NEW_GAME_FIELDS):
            raise SourceBoundaryError(
                f"new_game args must be {HOLDINGS_NEW_GAME_FIELDS}, got {tuple(sorted(kwargs.keys()))}"
            )
        args = {
            "country_id": _require_nonempty_str(kwargs["country_id"], "new_game.country_id", error_cls=SourceBoundaryError),
            "difficulty": _require_nonempty_str(kwargs["difficulty"], "new_game.difficulty", error_cls=SourceBoundaryError),
            "opening_mode": _require_nonempty_str(kwargs["opening_mode"], "new_game.opening_mode", error_cls=SourceBoundaryError),
            "seed": _require_int(kwargs["seed"], "new_game.seed", min_value=0, error_cls=SourceBoundaryError),
        }
    else:
        args = dict(kwargs)
    record = {"tick": t, "seq": s, "cmd": cmd_str, "args": args}
    validate_t2_command(record)
    return record


def validate_t2_command(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate that an external T2 command has exact fields (tick, seq, cmd, args) and no authority leak."""
    if not isinstance(record, Mapping):
        raise SourceBoundaryError("T2 command must be a mapping")
    if set(record.keys()) != set(T2_EXTERNAL_FIELDS):
        raise SourceBoundaryError(
            f"external T2 fields must be exactly {T2_EXTERNAL_FIELDS}, got {tuple(sorted(record.keys()))}"
        )
    tick = _require_int(record["tick"], "tick", min_value=0, error_cls=SourceBoundaryError)
    seq = _require_int(record["seq"], "seq", min_value=0, error_cls=SourceBoundaryError)
    cmd = _require_nonempty_str(record["cmd"], "cmd", error_cls=SourceBoundaryError)
    args = record["args"]
    if not isinstance(args, Mapping):
        raise SourceBoundaryError("T2 args must be a mapping")
    if cmd == "pause" and len(args) != 0:
        raise SourceBoundaryError("Holdings pause command must have empty args {}")
    if cmd == "new_game":
        if set(args.keys()) != set(HOLDINGS_NEW_GAME_FIELDS):
            raise SourceBoundaryError(f"Holdings new_game args must be exactly {HOLDINGS_NEW_GAME_FIELDS}")
        _require_nonempty_str(args["country_id"], "args.country_id", error_cls=SourceBoundaryError)
        _require_nonempty_str(args["difficulty"], "args.difficulty", error_cls=SourceBoundaryError)
        _require_nonempty_str(args["opening_mode"], "args.opening_mode", error_cls=SourceBoundaryError)
        _require_int(args["seed"], "args.seed", min_value=0, error_cls=SourceBoundaryError)
    return {"tick": tick, "seq": seq, "cmd": cmd, "args": copy.deepcopy(dict(args))}


def validate_source_observation(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a source-neutral observation without importing Godot or CDP adapters.

    ``source.kind == 'cdp'`` is reserved strictly for CDP-derived DOM/AX observations
    and rejects relabeled Godot scene trees.
    """
    if not isinstance(observation, Mapping):
        raise SourceBoundaryError("observation must be a mapping")
    obs = copy.deepcopy(dict(observation))
    source = obs.get("source")
    if not isinstance(source, Mapping):
        raise SourceBoundaryError("observation.source must be a mapping")
    kind = _require_nonempty_str(source.get("kind"), "source.kind", error_cls=SourceBoundaryError)
    if kind not in SUPPORTED_SOURCE_KINDS:
        raise SourceBoundaryError(f"unsupported source.kind: {kind!r}")
    if kind == "cdp":
        # Reject relabeled Godot trees masquerading as CDP DOM/AX
        if (
            "godot_tree" in obs
            or "scene_tree" in obs
            or source.get("origin") == "godot"
            or source.get("tree_type") == "godot_scene_tree"
            or (isinstance(obs.get("tree"), Mapping) and "godot_node_path" in obs["tree"])
        ):
            raise SourceBoundaryError("source.kind 'cdp' is reserved for CDP-derived DOM/AX, never a relabeled Godot tree")
        if "dom_ax" not in obs and "ax_nodes" not in obs and "dom_nodes" not in obs:
            raise SourceBoundaryError("source.kind 'cdp' requires CDP-derived DOM/AX payload (dom_ax, ax_nodes, or dom_nodes)")
    if "frame_digest" in obs:
        _require_sha256_hex(obs["frame_digest"], "observation.frame_digest", error_cls=SourceBoundaryError)
    return obs


class SourceNeutralBoundary:
    """Offline source-neutral harness for injected observations, T2 submissions, outcomes, and resets."""

    def __init__(self, *, trusted_authority: str = "trusted_injected_context") -> None:
        self.trusted_authority = _require_nonempty_str(trusted_authority, "trusted_authority", error_cls=SourceBoundaryError)
        self.electron_brokered_publisher = ELECTRON_BROKERED_PUBLISHER
        self.conductor_admission_binding = CONDUCTOR_ADMISSION_BINDING
        self.bookkeeping_seq = 0
        self.admitted_count = 0
        self.rejected_count = 0
        self.simulation_mutations = 0
        self.simulation_state: dict[str, Any] = {"paused": False, "game": None, "tick": 0}
        self.observations: list[dict[str, Any]] = []
        self.outcomes: list[dict[str, Any]] = []

    def ingest_observation(self, observation: Mapping[str, Any]) -> dict[str, Any]:
        validated = validate_source_observation(observation)
        self.observations.append(validated)
        return validated

    def submit_t2(self, command: Mapping[str, Any], *, trusted_context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Submit an external T2 command.

        Admission is not economic acceptance. Rejected attempts advance trusted
        bookkeeping without mutating simulation state.
        """
        self.bookkeeping_seq += 1
        ctx = dict(trusted_context or {"authority": self.trusted_authority, "authorized": True})
        try:
            clean_cmd = validate_t2_command(command)
        except SourceBoundaryError as exc:
            self.rejected_count += 1
            outcome = {
                "bookkeeping_seq": self.bookkeeping_seq,
                "admitted": False,
                "status": "REJECTED",
                "reason": exc.detail,
                "economic_acceptance": None,
                "confidence_status": AUSENTE,
                "simulation_mutated": False,
                "authority": ctx.get("authority", self.trusted_authority),
            }
            self.outcomes.append(outcome)
            return outcome

        if not ctx.get("authorized", True) or ctx.get("reject", False):
            self.rejected_count += 1
            outcome = {
                "bookkeeping_seq": self.bookkeeping_seq,
                "admitted": False,
                "status": "REJECTED",
                "reason": str(ctx.get("reason", "policy_refused")),
                "economic_acceptance": None,
                "confidence_status": AUSENTE,
                "simulation_mutated": False,
                "authority": ctx.get("authority", self.trusted_authority),
            }
            self.outcomes.append(outcome)
            return outcome

        self.admitted_count += 1
        self.simulation_mutations += 1
        self.simulation_state["tick"] = clean_cmd["tick"]
        if clean_cmd["cmd"] == "pause":
            self.simulation_state["paused"] = True
        elif clean_cmd["cmd"] == "new_game":
            self.simulation_state["game"] = dict(clean_cmd["args"])
            self.simulation_state["paused"] = False
        outcome = {
            "bookkeeping_seq": self.bookkeeping_seq,
            "admitted": True,
            "status": "ADMITTED",
            "reason": None,
            "economic_acceptance": None,
            "economic_acceptance_status": AUSENTE,
            "confidence_status": AUSENTE,
            "simulation_mutated": True,
            "authority": ctx.get("authority", self.trusted_authority),
            "command": clean_cmd,
        }
        self.outcomes.append(outcome)
        return outcome

    def reset(self) -> None:
        self.observations.clear()
        self.simulation_state = {"paused": False, "game": None, "tick": 0}


# ---------------------------------------------------------------------------
# Core Public API: describe_changes(baseline, candidate, protocol=None, ...)
# ---------------------------------------------------------------------------

def _extract_battery_metadata(
    battery: Any,
    side: str,
    default_env: Mapping[str, Any] | None,
    default_gen_sha: str | None,
) -> tuple[dict[str, str] | None, str | None, str | None, int | None, list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Extract environment, generator_sha256, algorithm_id, master_seed, cases, frames, and duplicate flags."""
    if not isinstance(battery, (Mapping, list, tuple)):
        raise HoldingsStatisticsError("E_BATTERY_TYPE", f"{side} must be a mapping or sequence of cases")

    manifest = battery.get("manifest") if isinstance(battery, Mapping) and isinstance(battery.get("manifest"), Mapping) else {}
    raw_env = None
    if isinstance(battery, Mapping):
        raw_env = battery.get("environment", manifest.get("environment"))
    if raw_env is None and default_env is not None:
        raw_env = default_env

    env: dict[str, str] | None = None
    env_valid = True
    if raw_env is not None:
        try:
            env = validate_environment(raw_env)
        except HoldingsStatisticsError:
            env = None
            env_valid = False

    gen_sha = None
    alg_id = None
    m_seed = None
    if isinstance(battery, Mapping):
        gen_sha = battery.get("generator_sha256", manifest.get("generator_sha256", default_gen_sha))
        alg_id = battery.get("algorithm_id", manifest.get("algorithm_id"))
        m_seed = battery.get("master_seed", manifest.get("master_seed"))
        if m_seed is not None and not _is_strict_int(m_seed):
            raise HoldingsStatisticsError("E_MASTER_SEED", f"{side}.master_seed must be an integer (bool rejected)")
        if m_seed is not None:
            m_seed = int(m_seed)
    else:
        gen_sha = default_gen_sha

    # Extract raw case collection
    raw_cases: Any = ()
    raw_frames: list[dict[str, Any]] = []
    explicit_dup_flag = False

    if isinstance(battery, (list, tuple)):
        raw_cases = battery
    elif isinstance(battery, Mapping):
        if battery.get("duplicated_correlated_frames") is True or battery.get("correlated_frame_duplicates") is True:
            explicit_dup_flag = True
        for key in ("cases", "case_results", "units", "windows", "episodes", "items", "rows"):
            if key in battery and battery[key] is not None:
                raw_cases = battery[key]
                break
        else:
            if "case_id" in battery:
                raw_cases = [battery]
            elif isinstance(manifest.get("cases"), (list, tuple)):
                raw_cases = manifest["cases"]
        top_frames = battery.get("frames", manifest.get("frames", ()))
        if isinstance(top_frames, (list, tuple)):
            for fr in top_frames:
                if isinstance(fr, Mapping):
                    raw_frames.append(dict(fr))

    cases_out: list[dict[str, Any]] = []
    if isinstance(raw_cases, Mapping):
        for cid, cval in raw_cases.items():
            if not isinstance(cval, Mapping):
                raise HoldingsStatisticsError("E_CASE_ENTRY", f"{side} case {cid!r} must be a mapping")
            d = copy.deepcopy(dict(cval))
            d.setdefault("case_id", str(cid))
            cases_out.append(d)
    elif isinstance(raw_cases, (list, tuple)):
        for idx, cval in enumerate(raw_cases):
            if is_dataclass(cval):
                d = {f.name: getattr(cval, f.name) for f in fields(cval)}
            elif isinstance(cval, Mapping):
                d = copy.deepcopy(dict(cval))
            else:
                raise HoldingsStatisticsError("E_CASE_ENTRY", f"{side} case at index {idx} must be a mapping")
            cases_out.append(d)
    else:
        raise HoldingsStatisticsError("E_CASES_COLLECTION", f"{side} cases must be a sequence or mapping")

    # If environment wasn't at battery root, check first case
    if env is None and env_valid and cases_out:
        first_env = cases_out[0].get("environment")
        if first_env is not None:
            try:
                env = validate_environment(first_env)
            except HoldingsStatisticsError:
                env = None
                env_valid = False
    if gen_sha is None and cases_out:
        gen_sha = cases_out[0].get("generator_sha256")
    if alg_id is None and cases_out:
        alg_id = cases_out[0].get("algorithm_id")
    if m_seed is None and cases_out and cases_out[0].get("master_seed") is not None:
        ms_val = cases_out[0].get("master_seed")
        if not _is_strict_int(ms_val):
            raise HoldingsStatisticsError("E_MASTER_SEED", "case.master_seed must be an integer")
        m_seed = int(ms_val)

    return env, gen_sha, alg_id, m_seed, cases_out, raw_frames, (explicit_dup_flag or not env_valid)


def _analyze_case_record(
    case: Mapping[str, Any],
    top_frames_for_case: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate and extract complete-case metrics, null counters, and exclusion/duplication flags."""
    cid_raw = case.get("case_id", case.get("id", case.get("scene_id", case.get("window_id"))))
    cid = _require_nonempty_str(cid_raw, "case_id")

    if case.get("labels_source") == "prediction" or case.get("from_predictions") is True:
        raise HoldingsStatisticsError("E_PREDICTION_LABELS", f"case {cid} derives expected labels from predictions (INV-J10-4)")
    labels_obj = case.get("labels", case.get("expected_labels"))
    if isinstance(labels_obj, Mapping) and labels_obj.get("source") == "prediction":
        raise HoldingsStatisticsError("E_PREDICTION_LABELS", f"case {cid} labels.source cannot be prediction (INV-J10-4)")

    # Check frame records for correlated frame duplication or sequence gaps
    case_frames: list[Any] = []
    if isinstance(case.get("frame_metadata"), (list, tuple)):
        case_frames.extend(case["frame_metadata"])
    elif isinstance(case.get("frames"), (list, tuple)):
        case_frames.extend(case["frames"])
    elif isinstance(case.get("frame_seqs"), (list, tuple)):
        case_frames.extend({"case_id": cid, "frame_seq": s} for s in case["frame_seqs"])
    if top_frames_for_case:
        case_frames.extend(top_frames_for_case)

    dup_frames = bool(
        case.get("duplicated_correlated_frames")
        or case.get("correlated_frame_duplicates")
        or case.get("duplicate_frames")
    )
    frame_gap = False
    seen_seqs: set[int] = set()
    seen_digests: set[tuple[str, str]] = set()
    last_seq: int | None = None
    unique_frame_count = 0

    for fr in case_frames:
        if isinstance(fr, Mapping):
            seq = fr.get("frame_seq", fr.get("seq"))
            if seq is not None:
                seq_i = _require_int(seq, f"case[{cid}].frame_seq", min_value=0)
                if seq_i in seen_seqs:
                    dup_frames = True
                    continue
                if last_seq is not None and seq_i != last_seq + 1:
                    if seq_i < last_seq:
                        dup_frames = True
                    else:
                        frame_gap = True
                seen_seqs.add(seq_i)
                last_seq = seq_i
            if fr.get("correlated_duplicate") is True or fr.get("duplicated") is True:
                dup_frames = True
                continue
            p_sha = fr.get("pixel_decl_sha256")
            r_sha = fr.get("raw_sha256")
            if isinstance(p_sha, str) and isinstance(r_sha, str):
                pair = (p_sha, r_sha)
                if pair in seen_digests and fr.get("allow_identical_decl") is not True:
                    # Identical pixel_decl_sha256 AND raw_sha256 indicates a duplicated frame record
                    dup_frames = True
                    continue
                seen_digests.add(pair)
            unique_frame_count += 1
        elif isinstance(fr, np.ndarray):
            if fr.dtype != np.uint8:
                raise HoldingsStatisticsError("E_FRAME_DTYPE", f"case {cid} frame must be uint8")
            unique_frame_count += 1

    # Check completeness & exclusions
    status_val = str(case.get("status", "OK")).upper() if case.get("status") is not None else "OK"
    complete_flag = case.get("complete", case.get("completed"))
    excluded_flag = bool(case.get("excluded", False))
    error_val = case.get("error")
    faults = tuple(case.get("faults", ()))

    expected_f_count = case.get("expected_frames")
    actual_f_count = case.get("frames") if _is_strict_int(case.get("frames")) else (unique_frame_count or None)

    exclusion_reason: str | None = None
    if excluded_flag:
        exclusion_reason = str(case.get("exclusion_reason") or "explicitly_excluded")
    elif complete_flag is False:
        exclusion_reason = str(case.get("exclusion_reason") or error_val or "incomplete_case")
    elif status_val in ("INCOMPLETE", "EXCLUDED", "FAULT", "FAILED", "ERROR", "REFUSED", "DROPPED", "SKIPPED", "GAP", "PLUGIN_EXCEPTION"):
        exclusion_reason = str(case.get("exclusion_reason") or error_val or status_val.lower())
    elif error_val is not None and str(error_val).strip():
        exclusion_reason = str(error_val)
    elif any(f in ("plugin_exception", "skip_frame", "unhandled_fault") for f in faults) and complete_flag is not True:
        exclusion_reason = f"fault:{faults[0]}"
    elif frame_gap and complete_flag is not True:
        exclusion_reason = "skipped_frames"
    elif expected_f_count is not None and actual_f_count is not None and int(actual_f_count) < int(expected_f_count):
        exclusion_reason = "incomplete_frame_sequence"

    is_complete = exclusion_reason is None

    # Extract metrics and operation counters
    metrics_in: dict[str, Any] = {}
    if isinstance(case.get("metrics"), Mapping):
        metrics_in.update(case["metrics"])
    if isinstance(case.get("counts"), Mapping):
        metrics_in.update(case["counts"])

    # Also support computing from expected/predicted arrays if supplied
    expected_arr = case.get("expected", case.get("truth"))
    pred_arr = case.get("predicted", case.get("predictions", case.get("claims")))
    if expected_arr is None and isinstance(labels_obj, (np.ndarray, list, tuple)):
        expected_arr = labels_obj
    if expected_arr is not None and pred_arr is not None:
        e_np = np.asarray(expected_arr, dtype=np.int64)
        p_np = np.asarray(pred_arr, dtype=np.int64)
        if e_np.shape != p_np.shape:
            raise HoldingsStatisticsError("E_ARRAY_SHAPE", f"case {cid} expected shape {e_np.shape} != predicted {p_np.shape}")
        if dup_frames and e_np.ndim >= 2 and e_np.shape[0] % 2 == 0 and e_np.shape[0] >= 2:
            half = e_np.shape[0] // 2
            if np.array_equal(e_np[:half], e_np[half:]) and np.array_equal(p_np[:half], p_np[half:]):
                e_np = e_np[:half]
                p_np = p_np[:half]
        metrics_in.setdefault("correct", int(np.count_nonzero(e_np == p_np)))
        metrics_in.setdefault("total", int(e_np.size))
        metrics_in.setdefault("errors", int(np.count_nonzero(e_np != p_np)))
        mv_m = e_np == 3
        metrics_in.setdefault("moved_correct", int(np.count_nonzero((p_np == 3) & mv_m)))
        metrics_in.setdefault("moved_total", int(np.count_nonzero(mv_m)))
        fl_m = e_np == 4
        metrics_in.setdefault("flicker_correct", int(np.count_nonzero((p_np == 4) & fl_m)))
        metrics_in.setdefault("flicker_total", int(np.count_nonzero(fl_m)))
        ch_m = e_np == 2
        metrics_in.setdefault("changed_correct", int(np.count_nonzero((p_np == 2) & ch_m)))
        metrics_in.setdefault("changed_total", int(np.count_nonzero(ch_m)))
        st_m = (e_np == 0) | (e_np == 1)
        metrics_in.setdefault("fp", int(np.count_nonzero((p_np >= 2) & st_m)))
        metrics_in.setdefault("fp_total", int(np.count_nonzero(st_m)))

    # Support top-level integer metric keys on case record
    for k in (
        "correct", "exact_matches", "matches", "hits",
        "total", "tile_frames", "n_tiles", "support",
        "errors", "mismatches", "fp", "fp_count", "false_positives",
        "fp_total", "fp_tile_frames", "fn", "fn_count", "false_negatives",
        "tp", "tn", "moved_correct", "mv_correct", "moved_total", "mv_total",
        "flicker_correct", "fl_correct", "flicker_total", "fl_total",
        "changed_correct", "changed_total", "settled",
    ):
        if k in case and k not in metrics_in:
            metrics_in[k] = case[k]

    # Canonicalize standard aliases without dropping original custom keys
    if "correct" not in metrics_in:
        for alias in ("exact_matches", "matches", "hits", "tp"):
            if alias in metrics_in:
                metrics_in["correct"] = metrics_in[alias]
                break
    if "total" not in metrics_in:
        for alias in ("tile_frames", "n_tiles", "support"):
            if alias in metrics_in:
                metrics_in["total"] = metrics_in[alias]
                break
    if "moved_correct" not in metrics_in and "mv_correct" in metrics_in:
        metrics_in["moved_correct"] = metrics_in["mv_correct"]
    if "moved_total" not in metrics_in and "mv_total" in metrics_in:
        metrics_in["moved_total"] = metrics_in["mv_total"]
    if "flicker_correct" not in metrics_in and "fl_correct" in metrics_in:
        metrics_in["flicker_correct"] = metrics_in["fl_correct"]
    if "flicker_total" not in metrics_in and "fl_total" in metrics_in:
        metrics_in["flicker_total"] = metrics_in["fl_total"]
    if "fp" not in metrics_in:
        for alias in ("fp_count", "false_positives"):
            if alias in metrics_in:
                metrics_in["fp"] = metrics_in[alias]
                break
    if "fp_total" not in metrics_in and "fp_tile_frames" in metrics_in:
        metrics_in["fp_total"] = metrics_in["fp_tile_frames"]

    if (
        "errors" not in metrics_in
        and _is_strict_int(metrics_in.get("total"))
        and _is_strict_int(metrics_in.get("correct"))
    ):
        metrics_in["errors"] = int(metrics_in["total"]) - int(metrics_in["correct"])

    clean_metrics: dict[str, int | None] = {}
    for mk, mv in metrics_in.items():
        if mv is None:
            clean_metrics[mk] = None
        elif isinstance(mv, (bool, np.bool_)):
            if mk == "settled":
                clean_metrics[mk] = 1 if mv else 0
            else:
                raise HoldingsStatisticsError("E_BOOL_COUNTER", f"case {cid} metric {mk!r} cannot be bool")
        elif _is_strict_int(mv):
            ival = int(mv)
            if ival < 0:
                raise HoldingsStatisticsError("E_NEGATIVE_COUNTER", f"case {cid} metric {mk!r} cannot be negative ({ival})")
            clean_metrics[mk] = ival
        else:
            raise HoldingsStatisticsError("E_METRIC_TYPE", f"case {cid} metric {mk!r} must be int or None")

    # Operation counters (gm, fl, etc.) - missing or None stays None!
    ops_raw = case.get("ops", case.get("operation_totals", {}))
    if not isinstance(ops_raw, Mapping):
        raise HoldingsStatisticsError("E_OPS_TYPE", f"case {cid} ops must be a mapping")
    clean_ops: dict[str, int | None] = {}
    for ok in set(ops_raw.keys()) | {"gm", "fl"}:
        if ok in ops_raw:
            ov = ops_raw[ok]
        elif f"{ok}_ops" in case:
            ov = case[f"{ok}_ops"]
        elif ok in case:
            ov = case[ok]
        else:
            ov = None
        if ov is None:
            clean_ops[ok] = None
        elif isinstance(ov, (bool, np.bool_)):
            raise HoldingsStatisticsError("E_BOOL_COUNTER", f"case {cid} op counter {ok!r} cannot be bool")
        elif _is_strict_int(ov):
            ival = int(ov)
            if ival < 0:
                raise HoldingsStatisticsError("E_NEGATIVE_COUNTER", f"case {cid} op counter {ok!r} cannot be negative")
            clean_ops[ok] = ival
        else:
            raise HoldingsStatisticsError("E_OPS_VALUE", f"case {cid} op counter {ok!r} must be int or None")

    # Also mirror gm_ops and fl_ops into clean_metrics for uniform counter lookup
    clean_metrics["gm_ops"] = clean_ops.get("gm", clean_ops.get("gm_ops"))
    clean_metrics["fl_ops"] = clean_ops.get("fl", clean_ops.get("fl_ops"))

    return {
        "case_id": cid,
        "complete": is_complete,
        "exclusion_reason": exclusion_reason,
        "duplicated_correlated_frames": dup_frames,
        "metrics": clean_metrics,
        "ops": clean_ops,
        "frames": actual_f_count if _is_strict_int(actual_f_count) else unique_frame_count,
    }


def describe_changes(
    baseline: Any,
    candidate: Any,
    protocol: Mapping[str, Any] | None = None,
    *,
    strict: bool = False,
    raise_on_refusal: bool = False,
    environment: Mapping[str, Any] | None = None,
    generator_sha256: str | None = None,
) -> ChangeDescription:
    """Compare baseline and candidate holdings runs over complete-case sampling units.

    Returns a ``ChangeDescription`` (mapping with attribute access) containing:
    - Descriptive integer differences (``differences`` / ``integer_differences``) and
      exact reduced rational fractions (``fractions`` / ``rational_fractions``).
    - Complete-case sampling units (``sampling_unit='complete_case'``, ``complete_case_count``).
    - Explicit ``None`` for unavailable evidence or missing operation counters (never 0).
    - Confidence status ``AUSENTE`` and explicit ``None`` for ``significance``, ``interval``,
      ``ope_certification``, and ``learned_reward`` when a reviewed protocol is absent.
    - Refusal of environment or generator mismatches, unequal batteries, and duplicated
      correlated frames, with per-case exclusions in ``per_case_exclusions`` and
      ``improvement_claimed=False``.
    """
    if protocol is not None and not isinstance(protocol, Mapping):
        raise ProtocolValidationError("protocol must be a mapping or None")
    protocol_copy = copy.deepcopy(dict(protocol)) if protocol is not None else None

    b_env, b_gen, b_alg, b_seed, b_cases, b_top_frames, b_bad_env = _extract_battery_metadata(
        baseline, "baseline", environment, generator_sha256
    )
    c_env, c_gen, c_alg, c_seed, c_cases, c_top_frames, c_bad_env = _extract_battery_metadata(
        candidate, "candidate", environment, generator_sha256
    )

    refusal_reasons: list[str] = []

    # 1. Check Environment Identity
    environment_match = True
    if b_bad_env or c_bad_env:
        environment_match = False
        refusal_reasons.append("environment_mismatch")
    elif b_env is not None and c_env is not None:
        if b_env != c_env:
            environment_match = False
            refusal_reasons.append("environment_mismatch")
    elif (b_env is None) != (c_env is None):
        environment_match = False
        refusal_reasons.append("environment_mismatch")

    # 2. Check Generator / Algorithm / Master Seed Identity
    generator_match = True
    if (b_gen is not None or c_gen is not None) and b_gen != c_gen:
        generator_match = False
        refusal_reasons.append("generator_mismatch")
    if (b_alg is not None or c_alg is not None) and b_alg != c_alg:
        generator_match = False
        if "generator_mismatch" not in refusal_reasons:
            refusal_reasons.append("generator_mismatch")
    if (b_seed is not None or c_seed is not None) and b_seed != c_seed:
        generator_match = False
        if "generator_mismatch" not in refusal_reasons:
            refusal_reasons.append("generator_mismatch")

    if (strict or raise_on_refusal) and not environment_match:
        raise EnvironmentMismatchError("baseline and candidate environment pins differ")
    if (strict or raise_on_refusal) and not generator_match:
        raise GeneratorMismatchError("baseline and candidate generator provenance differs")

    # Group top-level frames by case_id
    b_frames_by_case: dict[str, list[dict[str, Any]]] = {}
    for fr in b_top_frames:
        fcid = str(fr.get("case_id", ""))
        b_frames_by_case.setdefault(fcid, []).append(fr)
    c_frames_by_case: dict[str, list[dict[str, Any]]] = {}
    for fr in c_top_frames:
        fcid = str(fr.get("case_id", ""))
        c_frames_by_case.setdefault(fcid, []).append(fr)

    # Parse cases and detect duplicate case_ids (correlated case duplication)
    b_parsed: dict[str, dict[str, Any]] = {}
    c_parsed: dict[str, dict[str, Any]] = {}
    correlated_duplicates = bool(
        (isinstance(baseline, Mapping) and (baseline.get("duplicated_correlated_frames") or baseline.get("correlated_frame_duplicates")))
        or (isinstance(candidate, Mapping) and (candidate.get("duplicated_correlated_frames") or candidate.get("correlated_frame_duplicates")))
    )
    correlated_duplicate_cases: list[str] = []

    for rc in b_cases:
        info = _analyze_case_record(rc, b_frames_by_case.get(str(rc.get("case_id", "")), []))
        cid = info["case_id"]
        if cid in b_parsed:
            correlated_duplicates = True
            if cid not in correlated_duplicate_cases:
                correlated_duplicate_cases.append(cid)
            continue
        if info["duplicated_correlated_frames"]:
            correlated_duplicates = True
            if cid not in correlated_duplicate_cases:
                correlated_duplicate_cases.append(cid)
        b_parsed[cid] = info

    for rc in c_cases:
        info = _analyze_case_record(rc, c_frames_by_case.get(str(rc.get("case_id", "")), []))
        cid = info["case_id"]
        if cid in c_parsed:
            correlated_duplicates = True
            if cid not in correlated_duplicate_cases:
                correlated_duplicate_cases.append(cid)
            continue
        if info["duplicated_correlated_frames"]:
            correlated_duplicates = True
            if cid not in correlated_duplicate_cases:
                correlated_duplicate_cases.append(cid)
        c_parsed[cid] = info

    # Also detect if candidate/baseline inflated frame counts on the same case (e.g., 2x correlated frame duplication)
    for cid in sorted(set(b_parsed.keys()) & set(c_parsed.keys())):
        bf = b_parsed[cid]["frames"]
        cf = c_parsed[cid]["frames"]
        bt = b_parsed[cid]["metrics"].get("total")
        ct = c_parsed[cid]["metrics"].get("total")
        if _is_strict_int(bf) and _is_strict_int(cf) and bf > 0 and cf > 0 and bf != cf:
            correlated_duplicates = True
            if cid not in correlated_duplicate_cases:
                correlated_duplicate_cases.append(cid)
        elif _is_strict_int(bt) and _is_strict_int(ct) and bt > 0 and ct > 0 and bt != ct:
            # Unequal tile-frame count inside the same paired case indicates duplicated correlated frames or geometry mismatch
            correlated_duplicates = True
            if cid not in correlated_duplicate_cases:
                correlated_duplicate_cases.append(cid)

    # 3. Build Per-Case Exclusions & Check Battery Equality
    all_case_ids = sorted(set(b_parsed.keys()) | set(c_parsed.keys()))
    per_case_exclusions: dict[str, dict[str, Any]] = {}
    excluded_cases_list: list[dict[str, Any]] = []
    paired_complete_ids: list[str] = []
    unequal_battery = False

    if set(b_parsed.keys()) != set(c_parsed.keys()):
        unequal_battery = True

    for cid in all_case_ids:
        in_b = cid in b_parsed
        in_c = cid in c_parsed
        if not in_b:
            exc_entry = {
                "case_id": cid,
                "side": "baseline",
                "reason": "missing_in_baseline",
                "status": "EXCLUDED",
            }
            per_case_exclusions[cid] = exc_entry
            excluded_cases_list.append(exc_entry)
            unequal_battery = True
            continue
        if not in_c:
            exc_entry = {
                "case_id": cid,
                "side": "candidate",
                "reason": "missing_in_candidate",
                "status": "EXCLUDED",
            }
            per_case_exclusions[cid] = exc_entry
            excluded_cases_list.append(exc_entry)
            unequal_battery = True
            continue

        b_info = b_parsed[cid]
        c_info = c_parsed[cid]
        if not b_info["complete"] or not c_info["complete"]:
            if not b_info["complete"] and not c_info["complete"]:
                side = "both"
                reason = b_info["exclusion_reason"] or c_info["exclusion_reason"] or "incomplete_case"
            elif not b_info["complete"]:
                side = "baseline"
                reason = b_info["exclusion_reason"] or "incomplete_in_baseline"
                unequal_battery = True
            else:
                side = "candidate"
                reason = c_info["exclusion_reason"] or "incomplete_in_candidate"
                unequal_battery = True
            exc_entry = {
                "case_id": cid,
                "side": side,
                "reason": reason,
                "status": "EXCLUDED",
            }
            per_case_exclusions[cid] = exc_entry
            excluded_cases_list.append(exc_entry)
            continue

        paired_complete_ids.append(cid)

    if unequal_battery and "unequal_battery" not in refusal_reasons:
        refusal_reasons.append("unequal_battery")
    if correlated_duplicates and "correlated_frame_duplication" not in refusal_reasons:
        refusal_reasons.append("correlated_frame_duplication")
    if not paired_complete_ids and "no_complete_cases" not in refusal_reasons:
        refusal_reasons.append("no_complete_cases")

    if (strict or raise_on_refusal) and unequal_battery:
        raise BatteryMismatchError("baseline and candidate batteries are unequal")

    # 4. Evaluate Protocol Status & Confidence Status
    # Missing reviewed protocol cannot produce significance, an interval, OPE certification, or learned reward.
    protocol_reviewed = False
    protocol_status = AUSENTE
    if protocol_copy is not None:
        if "reviewed" in protocol_copy and not isinstance(protocol_copy["reviewed"], bool):
            raise ProtocolValidationError("protocol.reviewed must be a boolean")
        is_rev = protocol_copy.get("reviewed") is True
        has_id = isinstance(protocol_copy.get("protocol_id"), str) and bool(protocol_copy["protocol_id"].strip())
        theorem = protocol_copy.get("confidence_theorem", AUSENTE)
        if is_rev and has_id and theorem not in (None, AUSENTE, "DESIGNING", "UNPROVEN"):
            protocol_reviewed = True
            protocol_status = "REVIEWED"
        else:
            protocol_status = "UNREVIEWED"

    # Negative controls or mismatches or exclusions always keep confidence_status AUSENTE
    confidence_status = CONFIDENCE_STATUS_AUSENTE
    significance = None
    p_value = None
    interval = None
    ope_certification = None
    learned_reward = None
    reward = None
    reward_status = AUSENTE
    economic_improvement = None
    improvement_claimed = False
    evidence_strengthened = False

    # 5. Compute Complete-Case Descriptive Arithmetic & Rational Fractions
    # If environment or generator mismatched, refuse before pooling misaligned evidence
    if not environment_match or not generator_match:
        primary_error = "environment_mismatch" if not environment_match else "generator_mismatch"
        return ChangeDescription(
            schema=SCHEMA_VERSION,
            status="REFUSED",
            refused=True,
            accepted=False,
            error=primary_error,
            refusal_reasons=tuple(refusal_reasons),
            environment_match=environment_match,
            generator_match=generator_match,
            battery_equal=not unequal_battery,
            unequal_battery=unequal_battery,
            correlated_frame_duplicates=correlated_duplicates,
            correlated_duplicate_cases=tuple(correlated_duplicate_cases),
            evidence_strengthened=False,
            sampling_unit="complete_case",
            complete_case_count=0,
            sampling_unit_count=0,
            paired_case_ids=(),
            excluded_case_ids=tuple(sorted(per_case_exclusions.keys())),
            per_case_exclusions=per_case_exclusions,
            excluded_cases=tuple(excluded_cases_list),
            baseline_totals=None,
            candidate_totals=None,
            differences=None,
            integer_differences=None,
            fractions=None,
            rational_fractions=None,
            ops={"baseline": {"gm": None, "fl": None}, "candidate": {"gm": None, "fl": None}, "differences": {"gm": None, "fl": None}},
            per_case={},
            protocol_status=protocol_status,
            protocol_reviewed=False,
            confidence_status=CONFIDENCE_STATUS_AUSENTE,
            significance=None,
            p_value=None,
            interval=None,
            confidence_interval=None,
            ope_certification=None,
            learned_reward=None,
            reward=None,
            reward_status=AUSENTE,
            economic_improvement=None,
            improvement_claimed=False,
            improved=None,
            timing_scope=TIMING_SCOPE_VM,
        )

    # Collect all metric keys across paired complete cases
    all_metric_keys: set[str] = {
        "correct",
        "total",
        "errors",
        "fp",
        "fp_total",
        "moved_correct",
        "moved_total",
        "flicker_correct",
        "flicker_total",
        "changed_correct",
        "changed_total",
        "settled",
        "gm_ops",
        "fl_ops",
    }
    all_op_keys: set[str] = {"gm", "fl"}
    for cid in paired_complete_ids:
        all_metric_keys.update(b_parsed[cid]["metrics"].keys())
        all_metric_keys.update(c_parsed[cid]["metrics"].keys())
        all_op_keys.update(b_parsed[cid]["ops"].keys())
        all_op_keys.update(c_parsed[cid]["ops"].keys())

    baseline_totals: dict[str, int | None] = {}
    candidate_totals: dict[str, int | None] = {}
    differences: dict[str, int | None] = {}

    if not paired_complete_ids:
        for mk in sorted(all_metric_keys):
            baseline_totals[mk] = None
            candidate_totals[mk] = None
            differences[mk] = None
    else:
        for mk in sorted(all_metric_keys):
            b_vals = [b_parsed[cid]["metrics"].get(mk) for cid in paired_complete_ids]
            c_vals = [c_parsed[cid]["metrics"].get(mk) for cid in paired_complete_ids]
            if any(v is None for v in b_vals):
                baseline_totals[mk] = None
            else:
                baseline_totals[mk] = int(sum(int(v) for v in b_vals))
            if any(v is None for v in c_vals):
                candidate_totals[mk] = None
            else:
                candidate_totals[mk] = int(sum(int(v) for v in c_vals))
            if baseline_totals[mk] is None or candidate_totals[mk] is None:
                differences[mk] = None
            else:
                differences[mk] = int(candidate_totals[mk] - baseline_totals[mk])

    ops_baseline: dict[str, int | None] = {}
    ops_candidate: dict[str, int | None] = {}
    ops_diff: dict[str, int | None] = {}
    for ok in sorted(all_op_keys):
        if not paired_complete_ids:
            ops_baseline[ok] = None
            ops_candidate[ok] = None
            ops_diff[ok] = None
            continue
        b_ov = [b_parsed[cid]["ops"].get(ok) for cid in paired_complete_ids]
        c_ov = [c_parsed[cid]["ops"].get(ok) for cid in paired_complete_ids]
        ops_baseline[ok] = None if any(v is None for v in b_ov) else int(sum(int(v) for v in b_ov))
        ops_candidate[ok] = None if any(v is None for v in c_ov) else int(sum(int(v) for v in c_ov))
        if ops_baseline[ok] is None or ops_candidate[ok] is None:
            ops_diff[ok] = None
        else:
            ops_diff[ok] = int(ops_candidate[ok] - ops_baseline[ok])

    # Per-case breakdown and complete-case outcome classification
    per_case: dict[str, dict[str, Any]] = {}
    improved_case_count = 0
    regressed_case_count = 0
    unchanged_case_count = 0
    case_unit_b_acc_sum = Fraction(0, 1)
    case_unit_c_acc_sum = Fraction(0, 1)
    case_unit_acc_valid = bool(paired_complete_ids)

    for cid in paired_complete_ids:
        bm = b_parsed[cid]["metrics"]
        cm = c_parsed[cid]["metrics"]
        c_diffs: dict[str, int | None] = {}
        for mk in sorted(all_metric_keys):
            bv = bm.get(mk)
            cv = cm.get(mk)
            c_diffs[mk] = None if (bv is None or cv is None) else int(cv - bv)

        b_corr, b_tot = bm.get("correct"), bm.get("total")
        c_corr, c_tot = cm.get("correct"), cm.get("total")
        b_err, c_err = bm.get("errors"), cm.get("errors")

        case_b_frac = rational_fraction(b_corr, b_tot) if (b_corr is not None and b_tot is not None) else None
        case_c_frac = rational_fraction(c_corr, c_tot) if (c_corr is not None and c_tot is not None) else None
        case_delta_frac: RationalValue | None = None
        if case_b_frac is not None and case_c_frac is not None:
            df = case_c_frac.as_fraction() - case_b_frac.as_fraction()
            case_delta_frac = RationalValue(df.numerator, df.denominator)
            case_unit_b_acc_sum += case_b_frac.as_fraction()
            case_unit_c_acc_sum += case_c_frac.as_fraction()
            if df > 0:
                improved_case_count += 1
                outcome = "IMPROVED"
            elif df < 0:
                regressed_case_count += 1
                outcome = "REGRESSED"
            else:
                unchanged_case_count += 1
                outcome = "UNCHANGED"
        elif b_err is not None and c_err is not None:
            case_unit_acc_valid = False
            if c_err < b_err:
                improved_case_count += 1
                outcome = "IMPROVED"
            elif c_err > b_err:
                regressed_case_count += 1
                outcome = "REGRESSED"
            else:
                unchanged_case_count += 1
                outcome = "UNCHANGED"
        else:
            case_unit_acc_valid = False
            unchanged_case_count += 1
            outcome = "UNCHANGED"

        per_case[cid] = {
            "case_id": cid,
            "outcome": outcome,
            "baseline_metrics": dict(bm),
            "candidate_metrics": dict(cm),
            "differences": c_diffs,
            "baseline_ops": dict(b_parsed[cid]["ops"]),
            "candidate_ops": dict(c_parsed[cid]["ops"]),
            "ops_differences": {
                ok: (
                    None
                    if (b_parsed[cid]["ops"].get(ok) is None or c_parsed[cid]["ops"].get(ok) is None)
                    else int(c_parsed[cid]["ops"][ok] - b_parsed[cid]["ops"][ok])
                )
                for ok in sorted(all_op_keys)
            },
            "baseline_accuracy": case_b_frac,
            "candidate_accuracy": case_c_frac,
            "accuracy_difference": case_delta_frac,
        }

    n_units = len(paired_complete_ids)
    differences["improved_cases"] = improved_case_count if n_units > 0 else None
    differences["regressed_cases"] = regressed_case_count if n_units > 0 else None
    differences["unchanged_cases"] = unchanged_case_count if n_units > 0 else None
    differences["net_improved_cases"] = (improved_case_count - regressed_case_count) if n_units > 0 else None

    # Build exact reduced rational fractions over complete-case sampling units and pooled counts
    b_acc = rational_fraction(baseline_totals.get("correct"), baseline_totals.get("total"))
    c_acc = rational_fraction(candidate_totals.get("correct"), candidate_totals.get("total"))
    acc_diff: RationalValue | None = None
    if b_acc is not None and c_acc is not None:
        df = c_acc.as_fraction() - b_acc.as_fraction()
        acc_diff = RationalValue(df.numerator, df.denominator)

    unit_b_acc: RationalValue | None = None
    unit_c_acc: RationalValue | None = None
    unit_acc_diff: RationalValue | None = None
    if case_unit_acc_valid and n_units > 0:
        fb = case_unit_b_acc_sum / n_units
        fc = case_unit_c_acc_sum / n_units
        fd = fc - fb
        unit_b_acc = RationalValue(fb.numerator, fb.denominator)
        unit_c_acc = RationalValue(fc.numerator, fc.denominator)
        unit_acc_diff = RationalValue(fd.numerator, fd.denominator)

    mv_b = rational_fraction(baseline_totals.get("moved_correct"), baseline_totals.get("moved_total"))
    mv_c = rational_fraction(candidate_totals.get("moved_correct"), candidate_totals.get("moved_total"))
    mv_diff: RationalValue | None = None
    if mv_b is not None and mv_c is not None:
        df = mv_c.as_fraction() - mv_b.as_fraction()
        mv_diff = RationalValue(df.numerator, df.denominator)

    fl_b = rational_fraction(baseline_totals.get("flicker_correct"), baseline_totals.get("flicker_total"))
    fl_c = rational_fraction(candidate_totals.get("flicker_correct"), candidate_totals.get("flicker_total"))
    fl_diff: RationalValue | None = None
    if fl_b is not None and fl_c is not None:
        df = fl_c.as_fraction() - fl_b.as_fraction()
        fl_diff = RationalValue(df.numerator, df.denominator)

    fp_b = rational_fraction(baseline_totals.get("fp"), baseline_totals.get("fp_total"))
    fp_c = rational_fraction(candidate_totals.get("fp"), candidate_totals.get("fp_total"))
    fp_diff: RationalValue | None = None
    if fp_b is not None and fp_c is not None:
        df = fp_c.as_fraction() - fp_b.as_fraction()
        fp_diff = RationalValue(df.numerator, df.denominator)

    fractions: dict[str, RationalValue | None] = {
        "improved_cases": rational_fraction(improved_case_count, n_units),
        "regressed_cases": rational_fraction(regressed_case_count, n_units),
        "unchanged_cases": rational_fraction(unchanged_case_count, n_units),
        "net_case_change": rational_fraction(improved_case_count - regressed_case_count, n_units),
        "baseline_accuracy": b_acc,
        "candidate_accuracy": c_acc,
        "accuracy_difference": acc_diff,
        "case_unit_baseline_accuracy": unit_b_acc,
        "case_unit_candidate_accuracy": unit_c_acc,
        "case_unit_accuracy_difference": unit_acc_diff,
        "moved_recall_baseline": mv_b,
        "moved_recall_candidate": mv_c,
        "moved_recall_difference": mv_diff,
        "flicker_recall_baseline": fl_b,
        "flicker_recall_candidate": fl_c,
        "flicker_recall_difference": fl_diff,
        "fp_rate_baseline": fp_b,
        "fp_rate_candidate": fp_c,
        "fp_rate_difference": fp_diff,
    }

    is_refused = bool(refusal_reasons)
    status_str = "REFUSED" if is_refused else "OK"
    primary_err = refusal_reasons[0] if refusal_reasons else None

    return ChangeDescription(
        schema=SCHEMA_VERSION,
        status=status_str,
        refused=is_refused,
        accepted=False,
        error=primary_err,
        refusal_reasons=tuple(refusal_reasons),
        environment_match=environment_match,
        generator_match=generator_match,
        battery_equal=not unequal_battery,
        unequal_battery=unequal_battery,
        correlated_frame_duplicates=correlated_duplicates,
        correlated_duplicate_cases=tuple(correlated_duplicate_cases),
        evidence_strengthened=evidence_strengthened,
        sampling_unit="complete_case",
        complete_case_count=n_units,
        sampling_unit_count=n_units,
        paired_case_ids=tuple(paired_complete_ids),
        excluded_case_ids=tuple(sorted(per_case_exclusions.keys())),
        per_case_exclusions=per_case_exclusions,
        excluded_cases=tuple(excluded_cases_list),
        baseline_totals=baseline_totals,
        candidate_totals=candidate_totals,
        differences=differences,
        integer_differences=dict(differences),
        fractions=fractions,
        rational_fractions=dict(fractions),
        ops={
            "baseline": ops_baseline,
            "candidate": ops_candidate,
            "differences": ops_diff,
        },
        per_case=per_case,
        protocol_status=protocol_status,
        protocol_reviewed=protocol_reviewed,
        confidence_status=confidence_status,
        significance=significance,
        p_value=p_value,
        interval=interval,
        confidence_interval=interval,
        ope_certification=ope_certification,
        learned_reward=learned_reward,
        reward=reward,
        reward_status=reward_status,
        economic_improvement=economic_improvement,
        improvement_claimed=improvement_claimed,
        improved=None,
        timing_scope=TIMING_SCOPE_VM,
    )
