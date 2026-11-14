#!/usr/bin/env python3
"""Fail-closed static replay exporter and bundle validator for ViewPoint Swarm (H5_r1).

Reads authoritative `events.jsonl` (`swarm/CONTRACT.md` §5.2, `v == 1`), an
optional `<video>.meta.json` sidecar (`start_ts_ms` cutoff), and verified
still frames (`frame_<N>.png` + `frame_<N>.json`), emitting:
  1. `<out_dir>/static/frames/<replay_id>_f<NN>.png`
  2. `<out_dir>/replays/<replay_id>.json` (`viewpoint.replay/v1`)
  3. `<out_dir>/static/data/<replay_id>.swarm.json` (`viewpoint.swarm_timeline/v1`)
  4. `<out_dir>/PINS.manifest` (stored at viewer root outside `replays/` and `static/`)
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

# Resolve canonical `gateway.replay_store` when running inside the repository;
# fall back to the byte-identical vendored `replay_store.py` inside `/app`.
_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parents[1] if len(_THIS_DIR.parents) >= 2 else _THIS_DIR
if (_REPO_ROOT / "gateway" / "replay_store.py").is_file() and str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from gateway.replay_store import (  # type: ignore[import-not-found]
        ACTORS,
        FRAME_FIELDS,
        LABEL_FIELDS,
        MAX_FRAMES,
        MAX_REPLAY_BYTES,
        MAX_STEPS,
        PROVENANCE_FIELDS,
        REPLAY_ID_RE,
        SCHEMA as REPLAY_SCHEMA,
        SHA256_RE,
        STATUSES,
        STEP_FIELDS,
        TOP_LEVEL_FIELDS,
        VERSION_RE,
        Replay,
        ReplayError,
        ReplayNotFound,
        ReplayStore,
        _require_exact_fields,
        _require_int,
        _require_text,
        parse_pins,
    )
except ModuleNotFoundError:  # pragma: no cover - standalone `/app` container import
    try:
        from swarm.viewer.replay_store import (
            ACTORS,
            FRAME_FIELDS,
            LABEL_FIELDS,
            MAX_FRAMES,
            MAX_REPLAY_BYTES,
            MAX_STEPS,
            PROVENANCE_FIELDS,
            REPLAY_ID_RE,
            SCHEMA as REPLAY_SCHEMA,
            SHA256_RE,
            STATUSES,
            STEP_FIELDS,
            TOP_LEVEL_FIELDS,
            VERSION_RE,
            Replay,
            ReplayError,
            ReplayNotFound,
            ReplayStore,
            _require_exact_fields,
            _require_int,
            _require_text,
            parse_pins,
        )
    except ModuleNotFoundError:
        from replay_store import (  # type: ignore[import-not-found,no-redef]
            ACTORS,
            FRAME_FIELDS,
            LABEL_FIELDS,
            MAX_FRAMES,
            MAX_REPLAY_BYTES,
            MAX_STEPS,
            PROVENANCE_FIELDS,
            REPLAY_ID_RE,
            SCHEMA as REPLAY_SCHEMA,
            SHA256_RE,
            STATUSES,
            STEP_FIELDS,
            TOP_LEVEL_FIELDS,
            VERSION_RE,
            Replay,
            ReplayError,
            ReplayNotFound,
            ReplayStore,
            _require_exact_fields,
            _require_int,
            _require_text,
            parse_pins,
        )


SWARM_SCHEMA = "viewpoint.swarm_timeline/v1"
MAX_SWARM_BYTES = 4_000_000
VIDEO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.mp4$")
VIDEO_URL_RE = re.compile(r"^/static/videos/[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.mp4$")
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
SWARM_NOTE_PIN_RE = re.compile(r"(?:^|[\s;,])swarm_sha256=([^\s;,.\)]+)")
_HOME_ROOT = "/" + "home" + "/"
_HOME_SEG_RE = re.compile(_HOME_ROOT + r"[^/\s\"'\\]+")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

RING_ORDER: tuple[str, ...] = (
    "modeler-agent",
    "props-agent",
    "look-agent",
    "camera-agent",
)

# Canonical agent palette and viewport anchors per `swarm/CONTRACT.md` (§4.1, §5.2)
CANONICAL_AGENT_PALETTE: dict[str, dict[str, Any]] = {
    "modeler-agent": {
        "color": "#ff6b6b",
        "rgb": [255, 107, 107],
        "anchor_xy": [480, 420],
        "default_lane": "script",
        "allow_gui": False,
        "write_prefixes": ["col:CHAR_", "obj:CHAR_"],
    },
    "props-agent": {
        "color": "#4ecdc4",
        "rgb": [78, 205, 196],
        "anchor_xy": [1080, 620],
        "default_lane": "script",
        "allow_gui": False,
        "write_prefixes": ["col:PROPS_", "obj:PROP_"],
    },
    "look-agent": {
        "color": "#ffe66d",
        "rgb": [255, 230, 109],
        "anchor_xy": [780, 300],
        "default_lane": "script",
        "allow_gui": False,
        "write_prefixes": ["mat:", "light:", "world:"],
    },
    "camera-agent": {
        "color": "#38bdf8",
        "rgb": [56, 189, 248],
        "anchor_xy": [800, 500],
        "default_lane": "gui",
        "allow_gui": True,
        "write_prefixes": ["cam:", "render:"],
    },
}

VALID_REC_TYPES = frozenset({"claim", "enqueue", "turn", "close", "cursor", "control"})

# R8: Pure clean verified turns ONLY; VERIFIED_WITH_SPILL NEVER maps to "verified".
VERIFIED_VERDICTS = frozenset({"VERIFIED", "STATE_VERIFIED"})
SPILL_VERDICTS = frozenset({"VERIFIED_WITH_SPILL"})
REFUSED_VERDICTS = frozenset(
    {
        "JOB_ERROR",
        "STALE_FRAME",
        "FRAME_EVICTED",
        "NO_LEASE",
        "LEASE_EXPIRED",
        "ACTUATOR_BUSY",
        "FOOTPRINT_MISSING",
        "QUEUE_FULL",
        "DUPLICATE_EVENT",
        "CLAIM_REFUSED",
    }
)
PENDING_VERDICTS = frozenset({"INPUT_UNCERTAIN", "OUTCOME_UNCERTAIN", "TURN_TIMEOUT"})
OBSERVED_VERDICTS = frozenset({"NO_VISIBLE_CHANGE", "PASS"})
ALL_VERDICTS = (
    VERIFIED_VERDICTS
    | SPILL_VERDICTS
    | REFUSED_VERDICTS
    | PENDING_VERDICTS
    | OBSERVED_VERDICTS
)

SWARM_TOP_LEVEL_FIELDS = {
    "schema",
    "replay_id",
    "version",
    "mode",
    "live",
    "sample",
    "title",
    "task",
    "recorded_at",
    "window_start_ts_ms",
    "window_end_ts_ms",
    "duration_ms",
    "source_events_sha256",
    "video_url",
    "labels",
    "agent_palette",
    "frames",
    "claims",
    "turns",
    "cursors",
    "controls",
    "verdict_counts",
    "limitations",
}
SWARM_AGENT_FIELDS = {
    "color",
    "rgb",
    "anchor_xy",
    "default_lane",
    "allow_gui",
    "write_prefixes",
}
SWARM_FRAME_FIELDS = {
    "frame_id",
    "source_frame",
    "ts_ms",
    "t_ms",
    "src",
    "sha256",
    "width",
    "height",
    "view_epoch",
    "scene_epoch",
    "changed_region_count",
    "caption",
}
SWARM_CLAIM_FIELDS = {
    "agent_id",
    "lease_id",
    "lease_epoch",
    "ttl_ms",
    "issued_ts_ms",
    "expires_ts_ms",
    "t_ms",
    "allow_gui",
    "write_prefixes",
}
SWARM_TURN_FIELDS = {
    "seq",
    "turn",
    "turn_epoch",
    "tick",
    "ts_ms",
    "t_ms",
    "agent_id",
    "event_id",
    "lane",
    "kind",
    "verdict",
    "status",
    "rebased",
    "drops",
    "based_on_frame",
    "head_frame",
    "dispatch_capture_seq",
    "verify_frame_ref",
    "frame_id",
    "write_set",
    "expect",
    "reason",
    "own_region_count",
    "spill_region_count",
}
SWARM_CURSOR_FIELDS = {
    "seq",
    "ts_ms",
    "t_ms",
    "agent_id",
    "event_id",
    "phase",
    "x",
    "y",
    "frame",
    "frame_id",
    "label",
    "color",
}
SWARM_CONTROL_FIELDS = {
    "seq",
    "ts_ms",
    "t_ms",
    "op",
    "by",
    "reason",
}

DEFAULT_LIMITATIONS: tuple[str, ...] = (
    "Read-only REPLAY bundle (mode=REPLAY, live=false); viewer performs zero live Blender, X11, or broker actuation.",
    "Shared-UID host scope: local capsule processes ran under a shared host UID without kernel namespace or container isolation between agents and broker.",
    "Script-lane scene verification relies on Blender bridge outbox status=OK and disjoint write_prefixes claims; semantic 3D mesh diffing is AUSENTE.",
    "Cursor coordinates for script-lane events use deterministic per-agent viewport anchor slots; 3D-to-2D bpy projection is AUSENTE.",
    "No latency, cost, or generalized model-capability claim is asserted beyond the recorded SHA-256 pinned evidence.",
)


class ExportError(ReplayError):
    """Fail-closed error raised during replay export or swarm bundle validation."""

    def __init__(self, message: str, *, byte_offset: int | None = None) -> None:
        super().__init__(message)
        self.byte_offset = byte_offset


@dataclass(frozen=True)
class SourceFrame:
    frame_num: int
    ts_ms: int
    sha256: str
    width: int
    height: int
    view_epoch: int
    scene_epoch: int
    changed_region_count: int
    png_bytes: bytes


@dataclass(frozen=True)
class ExportResult:
    replay_id: str
    replay_path: Path
    replay_sha256: str
    swarm_path: Path
    swarm_sha256: str
    manifest_path: Path
    pins_path: Path
    swarm_pins_path: Path
    source_events_sha256: str
    frame_count: int
    step_count: int
    turn_count: int


def _require_nonbool_int_ge0(
    value: Any,
    field: str,
    *,
    byte_offset: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        loc = f" at byte_offset={byte_offset}" if byte_offset is not None else ""
        raise ExportError(f"{field} must be a non-boolean integer{loc}", byte_offset=byte_offset)
    if value < 0:
        loc = f" at byte_offset={byte_offset}" if byte_offset is not None else ""
        raise ExportError(f"{field} must be >= 0{loc}", byte_offset=byte_offset)
    return value


def _reject_symlink_chain(target: Path, root: Path, label: str) -> Path:
    root_resolved = root.resolve()
    probe: Path | None = target
    while probe is not None:
        try:
            if probe.is_symlink():
                raise ExportError(f"{label} must not be a symlink: {target}")
        except ExportError:
            raise
        except OSError as exc:
            raise ExportError(f"cannot stat {label} ({target}): {exc}") from None
        if probe == root or probe == root_resolved:
            break
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    resolved = target.resolve()
    if not resolved.is_relative_to(root_resolved):
        raise ExportError(f"{label} escapes directory {root}: {target}")
    return resolved


def _sanitize_host_paths(value: str) -> str:
    """Redact host home directory segments (`/<h>/<user>` -> `<home>`) in diagnostic strings."""
    cleaned = _HOME_SEG_RE.sub("<home>", value)
    if _HOME_ROOT in cleaned:
        cleaned = cleaned.replace(_HOME_ROOT, "<home>/")
    return cleaned


def _bounded_text(value: Any, *, fallback: str, limit: int) -> str:
    raw = _sanitize_host_paths(str(value if value is not None else "").replace("\x00", " ").strip())
    if not raw:
        raw = _sanitize_host_paths(fallback)
    if len(raw) > limit:
        return raw[: max(1, limit - 3)].rstrip() + "..."
    return raw


def _bounded_optional_text(value: Any, *, limit: int) -> str:
    raw = _sanitize_host_paths(str(value if value is not None else "").replace("\x00", " ").strip())
    if len(raw) > limit:
        return raw[: max(1, limit - 3)].rstrip() + "..."
    return raw


def extract_embedded_swarm_sha256(replay_doc: Mapping[str, Any], where: str = "replay") -> str:
    """Extract and validate the mandatory `swarm_sha256=<64-hex>` token in `provenance.note`."""
    prov = replay_doc.get("provenance")
    if not isinstance(prov, Mapping):
        raise ExportError(f"{where} provenance must be an object")
    note = prov.get("note")
    if not isinstance(note, str) or not note.strip() or len(note) > 600:
        raise ExportError(f"{where} provenance.note must be a non-empty string <= 600 chars")
    matches = SWARM_NOTE_PIN_RE.findall(note)
    if len(matches) != 1:
        raise ExportError(
            f"{where} provenance.note must contain exactly one swarm_sha256=<64-hex> token"
        )
    digest = matches[0]
    if not SHA256_RE.fullmatch(digest):
        raise ExportError(
            f"{where} provenance.note has malformed swarm_sha256 token: {digest!r}"
        )
    return digest


def load_pins_manifest(manifest_path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Load external `PINS.manifest` (`replay:<id>=<sha256>` / `swarm:<id>=<sha256>` or `<id>=<sha256>`)."""
    if manifest_path.is_symlink():
        raise ExportError(f"pins manifest must not be a symlink: {manifest_path}")
    if not manifest_path.is_file():
        raise ExportError(f"pins manifest not found: {manifest_path}")
    raw = manifest_path.read_bytes()
    if not raw:
        raise ExportError(f"pins manifest is empty: {manifest_path}", byte_offset=0)
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ExportError(f"pins manifest is not valid UTF-8: {exc}", byte_offset=exc.start) from None

    replay_tokens: list[str] = []
    swarm_tokens: list[str] = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            raise ExportError(f"{manifest_path}:{line_no}: blank line in pins manifest is not allowed")
        for part in stripped.split(","):
            token = part.strip()
            if not token:
                raise ExportError(f"{manifest_path}:{line_no}: empty pin token in pins manifest")
            if token.startswith("replay:"):
                replay_tokens.append(token.removeprefix("replay:"))
            elif token.startswith("swarm:"):
                swarm_tokens.append(token.removeprefix("swarm:"))
            else:
                replay_tokens.append(token)

    if not replay_tokens:
        raise ExportError(f"pins manifest contains zero replay pins: {manifest_path}")
    try:
        replay_pins = parse_pins(",".join(replay_tokens))
        swarm_pins = parse_pins(",".join(swarm_tokens)) if swarm_tokens else {}
    except ReplayError as exc:
        raise ExportError(f"{manifest_path}: {exc}") from None
    if not replay_pins:
        raise ExportError(f"pins manifest yielded zero replay pins: {manifest_path}")
    return replay_pins, swarm_pins


def load_pins_file(pins_path: Path) -> dict[str, str]:
    """Load replay pins from a pins file or `PINS.manifest` fail-closed."""
    replay_pins, _ = load_pins_manifest(pins_path)
    return replay_pins


def _write_pins_manifest(
    manifest_path: Path,
    replay_pins: Mapping[str, str],
    swarm_pins: Mapping[str, str],
) -> None:
    if manifest_path.is_symlink():
        raise ExportError(f"refusing to overwrite symlink manifest: {manifest_path}")
    lines: list[str] = []
    for rid in sorted(replay_pins):
        lines.append(f"replay:{rid}={replay_pins[rid].lower()}\n")
    for rid in sorted(swarm_pins):
        lines.append(f"swarm:{rid}={swarm_pins[rid].lower()}\n")
    content = "".join(lines).encode("utf-8")
    tmp_path = manifest_path.with_name(f".{manifest_path.name}.tmp.{os.getpid()}")
    tmp_path.write_bytes(content)
    os.replace(tmp_path, manifest_path)


def parse_events_jsonl(events_path: Path) -> list[dict[str, Any]]:
    """Fail-closed parser for `events.jsonl` (`swarm/CONTRACT.md` §5.2).

    Every line MUST end with `\\n`, decode as strict UTF-8, parse as a JSON `dict`
    with `v == 1` and a valid `rec` + non-boolean integer timestamp `>= 0`.
    Any violation raises `ExportError` naming the exact 0-based `byte_offset`.
    """
    if events_path.is_symlink():
        raise ExportError(f"events log must not be a symlink: {events_path}", byte_offset=0)
    if not events_path.is_file():
        raise ExportError(f"events log not found: {events_path}", byte_offset=0)

    raw = events_path.read_bytes()
    total_len = len(raw)
    if total_len == 0:
        raise ExportError(
            f"{events_path}: empty events log at byte_offset=0",
            byte_offset=0,
        )

    indexed: list[tuple[int, int, dict[str, Any]]] = []
    offset = 0
    line_no = 0

    while offset < total_len:
        line_no += 1
        nl_pos = raw.find(b"\n", offset)
        if nl_pos == -1:
            raise ExportError(
                f"{events_path}: line {line_no} at byte_offset={offset}: "
                f"torn final line missing trailing '\\n' (eof={total_len})",
                byte_offset=offset,
            )

        line_bytes = raw[offset:nl_pos]
        if line_bytes.endswith(b"\r"):
            line_bytes = line_bytes[:-1]

        if not line_bytes.strip():
            raise ExportError(
                f"{events_path}: line {line_no} at byte_offset={offset}: blank line is not allowed",
                byte_offset=offset,
            )

        try:
            line_text = line_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ExportError(
                f"{events_path}: line {line_no} at byte_offset={offset} "
                f"(bad_byte={offset + exc.start}): malformed UTF-8: {exc}",
                byte_offset=offset,
            ) from None

        try:
            rec = json.loads(line_text)
        except json.JSONDecodeError as exc:
            raise ExportError(
                f"{events_path}: line {line_no} at byte_offset={offset}: invalid JSON: {exc}",
                byte_offset=offset,
            ) from None

        if not isinstance(rec, dict):
            raise ExportError(
                f"{events_path}: line {line_no} at byte_offset={offset}: "
                f"record must be a JSON object, got {type(rec).__name__}",
                byte_offset=offset,
            )

        v_val = rec.get("v")
        if isinstance(v_val, bool) or v_val != 1:
            raise ExportError(
                f"{events_path}: line {line_no} at byte_offset={offset}: "
                f"unsupported record version v={v_val!r} (expected 1)",
                byte_offset=offset,
            )

        rec_type = rec.get("rec")
        if not isinstance(rec_type, str) or rec_type not in VALID_REC_TYPES:
            raise ExportError(
                f"{events_path}: line {line_no} at byte_offset={offset}: "
                f"unsupported rec={rec_type!r}",
                byte_offset=offset,
            )

        if rec_type == "claim":
            if "ts_ms" in rec:
                ts_ms = _require_nonbool_int_ge0(
                    rec["ts_ms"],
                    f"{events_path}:{line_no} claim.ts_ms",
                    byte_offset=offset,
                )
            elif "issued_ts_ms" in rec:
                ts_ms = _require_nonbool_int_ge0(
                    rec["issued_ts_ms"],
                    f"{events_path}:{line_no} claim.issued_ts_ms",
                    byte_offset=offset,
                )
            else:
                raise ExportError(
                    f"{events_path}: line {line_no} at byte_offset={offset}: "
                    "claim record missing issued_ts_ms/ts_ms",
                    byte_offset=offset,
                )
        else:
            if "ts_ms" not in rec:
                raise ExportError(
                    f"{events_path}: line {line_no} at byte_offset={offset}: "
                    f"{rec_type} record missing ts_ms",
                    byte_offset=offset,
                )
            ts_ms = _require_nonbool_int_ge0(
                rec["ts_ms"],
                f"{events_path}:{line_no} {rec_type}.ts_ms",
                byte_offset=offset,
            )

        if rec_type == "close":
            verdict = rec.get("verdict")
            if not isinstance(verdict, str) or verdict not in ALL_VERDICTS:
                raise ExportError(
                    f"{events_path}: line {line_no} at byte_offset={offset}: "
                    f"invalid close verdict={verdict!r}",
                    byte_offset=offset,
                )

        normalized = dict(rec)
        normalized["_ts_ms"] = ts_ms
        normalized["_line_no"] = line_no
        normalized["_byte_offset"] = offset
        indexed.append((ts_ms, line_no, normalized))

        offset = nl_pos + 1

    indexed.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in indexed]


def resolve_cutoff_ts_ms(
    *,
    sidecar_path: Path | None = None,
    min_ts_ms: int | None = None,
) -> int | None:
    """Resolve and validate `start_ts_ms` cutoff per H3 sidecar convention."""
    cutoff: int | None = None

    if sidecar_path is not None:
        if sidecar_path.is_symlink():
            raise ExportError(f"sidecar must not be a symlink: {sidecar_path}")
        if not sidecar_path.is_file():
            raise ExportError(f"sidecar file not found: {sidecar_path}")
        try:
            raw_sidecar = sidecar_path.read_bytes().decode("utf-8", errors="strict")
            sidecar_data = json.loads(raw_sidecar)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExportError(f"sidecar {sidecar_path} is not valid UTF-8 JSON: {exc}") from None
        if not isinstance(sidecar_data, dict):
            raise ExportError(f"sidecar {sidecar_path} must contain a JSON object")
        if "start_ts_ms" not in sidecar_data:
            raise ExportError(f"sidecar {sidecar_path} missing required integer start_ts_ms")
        cutoff = _require_nonbool_int_ge0(
            sidecar_data["start_ts_ms"],
            f"{sidecar_path.name}.start_ts_ms",
        )

    if min_ts_ms is not None:
        explicit_min = _require_nonbool_int_ge0(min_ts_ms, "min_ts_ms")
        cutoff = explicit_min if cutoff is None else max(cutoff, explicit_min)

    return cutoff


def _collect_referenced_frame_nums(records: Sequence[dict[str, Any]]) -> list[int]:
    refs: set[int] = set()
    for rec in records:
        rtype = rec["rec"]
        if rtype == "enqueue":
            val = rec.get("based_on_frame")
            if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
                refs.add(val)
        elif rtype == "turn":
            val = rec.get("head_frame")
            if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
                refs.add(val)
        elif rtype == "close":
            for key in ("dispatch_capture_seq", "verify_frame_ref"):
                val = rec.get(key)
                if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
                    refs.add(val)
        elif rtype == "cursor":
            val = rec.get("frame")
            if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
                refs.add(val)
    return sorted(refs)


def _load_source_frame(frames_dir: Path, frame_num: int) -> SourceFrame:
    if isinstance(frame_num, bool) or not isinstance(frame_num, int) or frame_num < 0:
        raise ExportError(f"invalid frame index: {frame_num!r}")
    png_name = f"frame_{frame_num}.png"
    json_name = f"frame_{frame_num}.json"
    if ".." in png_name or "/" in png_name or "\\" in png_name:
        raise ExportError(f"illegal frame path segment: {png_name}")

    png_path = _reject_symlink_chain(frames_dir / png_name, frames_dir, png_name)
    json_path = _reject_symlink_chain(frames_dir / json_name, frames_dir, json_name)

    if not png_path.is_file():
        raise ExportError(f"referenced frame PNG missing: {png_path}")
    if not json_path.is_file():
        raise ExportError(f"referenced frame metadata JSON missing: {json_path}")

    try:
        meta = json.loads(json_path.read_bytes().decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExportError(f"{json_path} is not valid UTF-8 JSON: {exc}") from None

    if not isinstance(meta, dict):
        raise ExportError(f"{json_path} must contain a JSON object")
    if meta.get("v") != 1 or isinstance(meta.get("v"), bool):
        raise ExportError(f"{json_path} unsupported v={meta.get('v')!r}")
    meta_frame = _require_nonbool_int_ge0(meta.get("frame"), f"{json_name}.frame")
    if meta_frame != frame_num:
        raise ExportError(f"{json_path} frame={meta_frame} does not match {frame_num}")

    expected_sha = meta.get("sha256")
    if not isinstance(expected_sha, str) or not SHA256_RE.fullmatch(expected_sha.lower()):
        raise ExportError(f"{json_path} has malformed sha256")
    expected_sha = expected_sha.lower()

    ts_ms = _require_nonbool_int_ge0(meta.get("ts_ms"), f"{json_name}.ts_ms")
    width = _require_nonbool_int_ge0(meta.get("width", 1920), f"{json_name}.width")
    height = _require_nonbool_int_ge0(meta.get("height", 1080), f"{json_name}.height")
    if width <= 0 or height <= 0:
        raise ExportError(f"{json_path} dimensions must be positive")
    view_epoch = _require_nonbool_int_ge0(meta.get("view_epoch", 0), f"{json_name}.view_epoch")
    scene_epoch = _require_nonbool_int_ge0(meta.get("scene_epoch", 0), f"{json_name}.scene_epoch")
    changed_regions = meta.get("changed_regions", [])
    if not isinstance(changed_regions, list):
        raise ExportError(f"{json_path} changed_regions must be a list")

    png_bytes = png_path.read_bytes()
    if len(png_bytes) < len(PNG_MAGIC) or not png_bytes.startswith(PNG_MAGIC):
        raise ExportError(f"{png_path} is not a valid PNG file")
    actual_sha = hashlib.sha256(png_bytes).hexdigest()
    if actual_sha != expected_sha:
        raise ExportError(
            f"{png_path} sha256 mismatch: expected {expected_sha}, got {actual_sha}"
        )

    return SourceFrame(
        frame_num=frame_num,
        ts_ms=ts_ms,
        sha256=actual_sha,
        width=width,
        height=height,
        view_epoch=view_epoch,
        scene_epoch=scene_epoch,
        changed_region_count=len(changed_regions),
        png_bytes=png_bytes,
    )


def _select_frames(
    source_frames: Sequence[SourceFrame],
    *,
    cutoff_ts_ms: int | None,
    max_frames: int = MAX_FRAMES,
    priority_frame_nums: frozenset[int] = frozenset(),
) -> list[SourceFrame]:
    """Order frames by monotonic `(ts_ms, frame_num)` and bound to `1..max_frames`."""
    ordered = sorted(source_frames, key=lambda f: (f.ts_ms, f.frame_num))
    if cutoff_ts_ms is not None:
        ordered = [f for f in ordered if f.ts_ms >= cutoff_ts_ms]

    if not ordered:
        if cutoff_ts_ms is not None:
            raise ExportError(
                f"zero eligible source frames satisfy cutoff start_ts_ms={cutoff_ts_ms}"
            )
        raise ExportError("no valid frames available to export")

    if len(ordered) <= max_frames:
        return ordered

    # First deduplicate consecutive identical SHA-256 frames (always retaining endpoints and priority refs).
    deduped: list[SourceFrame] = [ordered[0]]
    for item in ordered[1:-1]:
        if item.sha256 != deduped[-1].sha256 or item.frame_num in priority_frame_nums:
            deduped.append(item)
    if ordered[-1].frame_num != deduped[-1].frame_num:
        deduped.append(ordered[-1])

    if len(deduped) <= max_frames:
        return deduped

    if max_frames == 1:
        return [deduped[-1]]

    # Preserve endpoints + priority frames (spill / error / guard frames), then evenly sample remaining budget.
    must_keep: set[int] = {0, len(deduped) - 1}
    for idx, sf in enumerate(deduped):
        if sf.frame_num in priority_frame_nums and len(must_keep) < max_frames:
            must_keep.add(idx)

    span = len(deduped) - 1
    for i in range(max_frames):
        if len(must_keep) >= max_frames:
            break
        idx = int(round((i * span) / (max_frames - 1)))
        must_keep.add(idx)

    # Fill any collision gaps deterministically from remaining indices
    if len(must_keep) < max_frames:
        for idx in range(len(deduped)):
            if len(must_keep) >= max_frames:
                break
            must_keep.add(idx)

    return [deduped[i] for i in sorted(must_keep)[:max_frames]]


def _verdict_to_replay_status(verdict: str) -> str:
    """Map broker verdict to `viewpoint.replay/v1` status (`VERIFIED_WITH_SPILL` -> `'observed'`, NEVER `'verified'`)."""
    if verdict in VERIFIED_VERDICTS:
        return "verified"
    if verdict in SPILL_VERDICTS:
        return "observed"
    if verdict in REFUSED_VERDICTS:
        return "refused"
    if verdict in PENDING_VERDICTS:
        return "pending"
    return "observed"


def _normalize_video_url(video_arg: str | None) -> str | None:
    if video_arg is None:
        return None
    raw = video_arg.strip()
    if not raw:
        return None
    if ".." in raw or "\x00" in raw:
        raise ExportError(f"invalid --video argument (traversal forbidden): {video_arg!r}")
    name = Path(raw).name
    if not VIDEO_NAME_RE.fullmatch(name):
        raise ExportError(
            f"--video filename {name!r} must match {VIDEO_NAME_RE.pattern}"
        )
    return f"/static/videos/{name}"


def _resolve_frame_id_for_ref(
    frame_ref: int | None,
    rel_t_ms: int,
    frame_id_by_num: Mapping[int, str],
    frame_t_list: Sequence[int],
    frame_id_list: Sequence[str],
) -> str:
    if frame_ref is not None and frame_ref in frame_id_by_num:
        return frame_id_by_num[frame_ref]
    pos = bisect.bisect_right(frame_t_list, rel_t_ms) - 1
    if pos < 0:
        pos = 0
    return frame_id_list[pos]


def validate_swarm_bundle(
    bundle_or_path: dict[str, Any] | Path,
    *,
    static_dir: Path | None = None,
    replay_dir: Path | None = None,
    pins: Mapping[str, str] | None = None,
    swarm_pins: Mapping[str, str] | None = None,
    require_pin: bool = True,
) -> dict[str, Any]:
    """Fail-closed validator for `viewpoint.swarm_timeline/v1` + external SHA-256 pins."""
    swarm_path: Path | None = None
    swarm_digest: str | None = None

    if isinstance(bundle_or_path, Path):
        swarm_path = bundle_or_path
        if swarm_path.is_symlink():
            raise ExportError(f"swarm bundle must not be a symlink: {swarm_path}")
        if not swarm_path.is_file():
            raise ExportError(f"swarm bundle not found: {swarm_path}")
        raw = swarm_path.read_bytes()
        if len(raw) > MAX_SWARM_BYTES:
            raise ExportError(f"{swarm_path.name} exceeds {MAX_SWARM_BYTES} bytes")
        try:
            data = json.loads(raw.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExportError(f"{swarm_path.name} is not valid UTF-8 JSON: {exc}") from None
        swarm_digest = hashlib.sha256(raw).hexdigest()
        if static_dir is None:
            static_dir = swarm_path.parent.parent
        viewer_root = static_dir.parent
        if replay_dir is None and (viewer_root / "replays").is_dir():
            replay_dir = viewer_root / "replays"
        if require_pin and pins is None:
            manifest_path = viewer_root / "PINS.manifest"
            m_replay_pins, m_swarm_pins = load_pins_manifest(manifest_path)
            pins = m_replay_pins
            if swarm_pins is None:
                swarm_pins = m_swarm_pins
    elif isinstance(bundle_or_path, dict):
        data = bundle_or_path
    else:
        raise ExportError("swarm bundle must be a Path or dict")

    if not isinstance(data, dict):
        raise ExportError("swarm bundle root must be a JSON object")

    where_root = f"{swarm_path.name} top level" if swarm_path is not None else "swarm bundle top level"
    try:
        _require_exact_fields(data, SWARM_TOP_LEVEL_FIELDS, where_root)
    except ReplayError as exc:
        raise ExportError(str(exc)) from None

    if data["schema"] != SWARM_SCHEMA:
        raise ExportError(f"unsupported swarm schema: {data['schema']!r}")

    try:
        replay_id = _require_text(data["replay_id"], "replay_id", limit=64)
        if not REPLAY_ID_RE.fullmatch(replay_id):
            raise ExportError(f"invalid replay_id: {replay_id!r}")
        version = _require_text(data["version"], "version", limit=32)
        if not VERSION_RE.fullmatch(version):
            raise ExportError(f"invalid version: {version!r}")
    except ReplayError as exc:
        raise ExportError(str(exc)) from None

    if data["mode"] != "REPLAY":
        raise ExportError("swarm bundle mode must be REPLAY")
    if data["live"] is not False:
        raise ExportError("swarm bundle live must be False")
    if not isinstance(data["sample"], bool):
        raise ExportError("swarm bundle sample must be a boolean")

    labels = data["labels"]
    if not isinstance(labels, dict):
        raise ExportError("labels must be an object")
    try:
        _require_exact_fields(labels, LABEL_FIELDS, "labels")
    except ReplayError as exc:
        raise ExportError(str(exc)) from None
    if labels.get("mode") != "REPLAY" or labels.get("live") is not False:
        raise ExportError("labels must declare mode=REPLAY and live=false")
    if labels.get("sample") is not data["sample"]:
        raise ExportError("labels.sample must match sample")

    try:
        _require_text(data["title"], "title", limit=240)
        _require_text(data["task"], "task", limit=2000)
        _require_text(data["recorded_at"], "recorded_at", limit=64)
        win_start = _require_int(data["window_start_ts_ms"], "window_start_ts_ms", minimum=0)
        win_end = _require_int(data["window_end_ts_ms"], "window_end_ts_ms", minimum=0)
        duration_ms = _require_int(data["duration_ms"], "duration_ms", minimum=0)
    except ReplayError as exc:
        raise ExportError(str(exc)) from None

    if win_end < win_start or duration_ms != (win_end - win_start):
        raise ExportError("window_end_ts_ms and duration_ms are inconsistent with window_start_ts_ms")

    source_events_sha256 = data["source_events_sha256"]
    if not isinstance(source_events_sha256, str) or not SHA256_RE.fullmatch(source_events_sha256):
        raise ExportError("source_events_sha256 must be 64-char lowercase hex")

    video_url = data["video_url"]
    if video_url is not None:
        if not isinstance(video_url, str) or ".." in video_url or not VIDEO_URL_RE.fullmatch(video_url):
            raise ExportError(f"invalid video_url: {video_url!r}")

    palette = data["agent_palette"]
    if not isinstance(palette, dict):
        raise ExportError("agent_palette must be an object")
    try:
        _require_exact_fields(palette, set(RING_ORDER), "agent_palette")
    except ReplayError as exc:
        raise ExportError(str(exc)) from None
    for agent_id, expected_spec in CANONICAL_AGENT_PALETTE.items():
        entry = palette[agent_id]
        if not isinstance(entry, dict):
            raise ExportError(f"agent_palette.{agent_id} must be an object")
        try:
            _require_exact_fields(entry, SWARM_AGENT_FIELDS, f"agent_palette.{agent_id}")
        except ReplayError as exc:
            raise ExportError(str(exc)) from None
        if entry["color"] != expected_spec["color"]:
            raise ExportError(
                f"agent_palette.{agent_id}.color must be {expected_spec['color']}, got {entry['color']!r}"
            )
        if entry["rgb"] != expected_spec["rgb"] or entry["anchor_xy"] != expected_spec["anchor_xy"]:
            raise ExportError(f"agent_palette.{agent_id} rgb/anchor_xy mismatch")
        if entry["allow_gui"] is not expected_spec["allow_gui"]:
            raise ExportError(f"agent_palette.{agent_id}.allow_gui mismatch")
        if entry["write_prefixes"] != expected_spec["write_prefixes"]:
            raise ExportError(f"agent_palette.{agent_id}.write_prefixes mismatch")

    frames = data["frames"]
    if not isinstance(frames, list) or not 1 <= len(frames) <= MAX_FRAMES:
        raise ExportError(f"swarm frames must contain 1..{MAX_FRAMES} entries")
    frame_ids: set[str] = set()
    last_frame_t = -1
    resolved_static = static_dir.resolve() if static_dir is not None else None
    for idx, frame in enumerate(frames):
        where = f"swarm.frames[{idx}]"
        if not isinstance(frame, dict):
            raise ExportError(f"{where} must be an object")
        try:
            _require_exact_fields(frame, SWARM_FRAME_FIELDS, where)
            fid = _require_text(frame["frame_id"], f"{where}.frame_id", limit=32)
            _require_int(frame["source_frame"], f"{where}.source_frame", minimum=0)
            _require_int(frame["ts_ms"], f"{where}.ts_ms", minimum=0)
            t_ms = _require_int(frame["t_ms"], f"{where}.t_ms", minimum=0)
            src = _require_text(frame["src"], f"{where}.src", limit=300)
            digest = _require_text(frame["sha256"], f"{where}.sha256", limit=64)
            _require_int(frame["width"], f"{where}.width", minimum=1)
            _require_int(frame["height"], f"{where}.height", minimum=1)
            _require_int(frame["view_epoch"], f"{where}.view_epoch", minimum=0)
            _require_int(frame["scene_epoch"], f"{where}.scene_epoch", minimum=0)
            _require_int(frame["changed_region_count"], f"{where}.changed_region_count", minimum=0)
            _require_text(frame["caption"], f"{where}.caption", limit=400)
        except ReplayError as exc:
            raise ExportError(str(exc)) from None
        if fid in frame_ids:
            raise ExportError(f"duplicate frame_id in swarm bundle: {fid}")
        frame_ids.add(fid)
        if t_ms < last_frame_t:
            raise ExportError("swarm frames must be ordered by t_ms")
        last_frame_t = t_ms
        rel_frame = src.removeprefix("/static/frames/")
        if not src.startswith("/static/frames/") or ".." in src or "/" in rel_frame or not src.lower().endswith(".png"):
            raise ExportError(f"{where}.src must be a flat /static/frames/<name>.png asset: {src!r}")
        if not SHA256_RE.fullmatch(digest):
            raise ExportError(f"{where}.sha256 invalid")
        if resolved_static is not None:
            rel = src.removeprefix("/static/")
            candidate = _reject_symlink_chain(resolved_static / rel, resolved_static, f"{where}.src")
            if not candidate.is_file():
                raise ExportError(f"{where}.src missing on disk: {candidate}")
            actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
            if actual != digest:
                raise ExportError(f"{where}.sha256 does not match disk asset {src}")

    claims = data["claims"]
    if not isinstance(claims, list) or len(claims) > 128:
        raise ExportError("claims must be a list of <= 128 entries")
    for idx, claim in enumerate(claims):
        where = f"claims[{idx}]"
        if not isinstance(claim, dict):
            raise ExportError(f"{where} must be an object")
        try:
            _require_exact_fields(claim, SWARM_CLAIM_FIELDS, where)
            _require_text(claim["agent_id"], f"{where}.agent_id", limit=64)
            _require_text(claim["lease_id"], f"{where}.lease_id", limit=128)
            _require_int(claim["lease_epoch"], f"{where}.lease_epoch", minimum=0)
            _require_int(claim["ttl_ms"], f"{where}.ttl_ms", minimum=0)
            _require_int(claim["issued_ts_ms"], f"{where}.issued_ts_ms", minimum=0)
            _require_int(claim["expires_ts_ms"], f"{where}.expires_ts_ms", minimum=0)
            _require_int(claim["t_ms"], f"{where}.t_ms", minimum=0)
        except ReplayError as exc:
            raise ExportError(str(exc)) from None
        if not isinstance(claim["allow_gui"], bool):
            raise ExportError(f"{where}.allow_gui must be a boolean")
        if not isinstance(claim["write_prefixes"], list) or not all(
            isinstance(p, str) for p in claim["write_prefixes"]
        ):
            raise ExportError(f"{where}.write_prefixes must be list[str]")

    turns = data["turns"]
    if not isinstance(turns, list) or not 1 <= len(turns) <= 1000:
        raise ExportError("turns must be a list of 1..1000 entries")
    last_turn_t = -1
    for idx, turn_rec in enumerate(turns):
        where = f"turns[{idx}]"
        if not isinstance(turn_rec, dict):
            raise ExportError(f"{where} must be an object")
        try:
            _require_exact_fields(turn_rec, SWARM_TURN_FIELDS, where)
            _require_int(turn_rec["seq"], f"{where}.seq", minimum=1)
            _require_int(turn_rec["ts_ms"], f"{where}.ts_ms", minimum=0)
            t_ms = _require_int(turn_rec["t_ms"], f"{where}.t_ms", minimum=0)
            _require_text(turn_rec["agent_id"], f"{where}.agent_id", limit=64)
            verdict = _require_text(turn_rec["verdict"], f"{where}.verdict", limit=64)
            status = _require_text(turn_rec["status"], f"{where}.status", limit=32)
            _require_int(turn_rec["drops"], f"{where}.drops", minimum=0)
            fid = _require_text(turn_rec["frame_id"], f"{where}.frame_id", limit=32)
            _require_int(turn_rec["own_region_count"], f"{where}.own_region_count", minimum=0)
            _require_int(turn_rec["spill_region_count"], f"{where}.spill_region_count", minimum=0)
        except ReplayError as exc:
            raise ExportError(str(exc)) from None
        if t_ms < last_turn_t:
            raise ExportError("turns must be ordered by t_ms")
        last_turn_t = t_ms
        if verdict not in ALL_VERDICTS and verdict != "QUEUED":
            raise ExportError(f"{where}.verdict unsupported: {verdict!r}")
        if status not in STATUSES:
            raise ExportError(f"{where}.status unsupported: {status!r}")
        if verdict == "VERIFIED_WITH_SPILL" and status == "verified":
            raise ExportError(f"{where}: VERIFIED_WITH_SPILL must never map to status='verified'")
        expected_status = "pending" if verdict == "QUEUED" else _verdict_to_replay_status(verdict)
        if status != expected_status:
            raise ExportError(f"{where}.status {status!r} does not match expected {expected_status!r} for {verdict}")
        if not isinstance(turn_rec["rebased"], bool):
            raise ExportError(f"{where}.rebased must be boolean")
        if fid not in frame_ids:
            raise ExportError(f"{where}.frame_id unknown: {fid}")
        if not isinstance(turn_rec["expect"], str) or len(turn_rec["expect"]) > 1000:
            raise ExportError(f"{where}.expect must be str <= 1000 chars")
        if not isinstance(turn_rec["reason"], str) or len(turn_rec["reason"]) > 1000:
            raise ExportError(f"{where}.reason must be str <= 1000 chars")
        if not isinstance(turn_rec["write_set"], list) or not all(
            isinstance(w, str) for w in turn_rec["write_set"]
        ):
            raise ExportError(f"{where}.write_set must be list[str]")

    cursors = data["cursors"]
    if not isinstance(cursors, list) or len(cursors) > 2000:
        raise ExportError("cursors must be a list of <= 2000 entries")
    last_cur_t = -1
    for idx, cur in enumerate(cursors):
        where = f"cursors[{idx}]"
        if not isinstance(cur, dict):
            raise ExportError(f"{where} must be an object")
        try:
            _require_exact_fields(cur, SWARM_CURSOR_FIELDS, where)
            _require_int(cur["seq"], f"{where}.seq", minimum=1)
            _require_int(cur["ts_ms"], f"{where}.ts_ms", minimum=0)
            t_ms = _require_int(cur["t_ms"], f"{where}.t_ms", minimum=0)
            _require_text(cur["agent_id"], f"{where}.agent_id", limit=64)
            phase = _require_text(cur["phase"], f"{where}.phase", limit=16)
            _require_int(cur["x"], f"{where}.x", minimum=0)
            _require_int(cur["y"], f"{where}.y", minimum=0)
            _require_int(cur["frame"], f"{where}.frame", minimum=0)
            fid = _require_text(cur["frame_id"], f"{where}.frame_id", limit=32)
            _require_text(cur["label"], f"{where}.label", limit=300)
            color = _require_text(cur["color"], f"{where}.color", limit=16)
        except ReplayError as exc:
            raise ExportError(str(exc)) from None
        if t_ms < last_cur_t:
            raise ExportError("cursors must be ordered by t_ms")
        last_cur_t = t_ms
        if phase not in {"intent", "act", "result"}:
            raise ExportError(f"{where}.phase invalid: {phase!r}")
        if fid not in frame_ids:
            raise ExportError(f"{where}.frame_id unknown: {fid}")
        if not HEX_COLOR_RE.fullmatch(color):
            raise ExportError(f"{where}.color invalid hex: {color!r}")

    controls = data["controls"]
    if not isinstance(controls, list) or len(controls) > 200:
        raise ExportError("controls must be a list of <= 200 entries")
    for idx, ctrl in enumerate(controls):
        where = f"controls[{idx}]"
        if not isinstance(ctrl, dict):
            raise ExportError(f"{where} must be an object")
        try:
            _require_exact_fields(ctrl, SWARM_CONTROL_FIELDS, where)
            _require_int(ctrl["seq"], f"{where}.seq", minimum=1)
            _require_int(ctrl["ts_ms"], f"{where}.ts_ms", minimum=0)
            _require_int(ctrl["t_ms"], f"{where}.t_ms", minimum=0)
            op = _require_text(ctrl["op"], f"{where}.op", limit=32)
            by = _require_text(ctrl["by"], f"{where}.by", limit=64)
        except ReplayError as exc:
            raise ExportError(str(exc)) from None
        if op not in {"stop", "resume", "reconcile", "park", "checkpoint"}:
            raise ExportError(f"{where}.op invalid: {op!r}")
        if by not in {"director-agent", "broker", "operator"}:
            raise ExportError(f"{where}.by invalid: {by!r}")
        if not isinstance(ctrl["reason"], str) or len(ctrl["reason"]) > 600:
            raise ExportError(f"{where}.reason must be str <= 600 chars")

    verdict_counts = data["verdict_counts"]
    if not isinstance(verdict_counts, dict):
        raise ExportError("verdict_counts must be an object")
    for k, v in verdict_counts.items():
        if k not in ALL_VERDICTS and k != "QUEUED":
            raise ExportError(f"verdict_counts has unknown verdict {k!r}")
        _require_nonbool_int_ge0(v, f"verdict_counts.{k}")

    limitations = data["limitations"]
    if not isinstance(limitations, list) or not 1 <= len(limitations) <= 20:
        raise ExportError("limitations must contain 1..20 entries")
    for idx, item in enumerate(limitations):
        try:
            _require_text(item, f"limitations[{idx}]", limit=400)
        except ReplayError as exc:
            raise ExportError(str(exc)) from None

    expected_sha_tag = f"source events.jsonl sha256={source_events_sha256}"
    if not any(expected_sha_tag in item for item in limitations):
        raise ExportError("limitations missing required source events.jsonl sha256 disclosure")

    expected_replay_pin: str | None = None
    if require_pin:
        if pins is None or replay_id not in pins:
            raise ExportError(f"missing replay SHA-256 pin for {replay_id}")
        expected_replay_pin = pins[replay_id].lower()
        if not SHA256_RE.fullmatch(expected_replay_pin):
            raise ExportError(f"malformed replay SHA-256 pin for {replay_id}")

    if swarm_path is not None and replay_dir is None:
        raise ExportError(
            f"paired replay_dir is required to verify embedded swarm_sha256 for {swarm_path}"
        )
    if replay_dir is not None:
        replay_file = _reject_symlink_chain(
            replay_dir / f"{replay_id}.json",
            replay_dir,
            f"replays/{replay_id}.json",
        )
        if not replay_file.is_file():
            raise ExportError(f"paired replay JSON missing: {replay_file}")
        replay_raw = replay_file.read_bytes()
        if expected_replay_pin is not None:
            actual_replay_sha = hashlib.sha256(replay_raw).hexdigest()
            if actual_replay_sha != expected_replay_pin:
                raise ExportError(
                    f"{replay_file.name} sha256 ({actual_replay_sha}) does not match pinned ({expected_replay_pin})"
                )
        try:
            replay_doc = json.loads(replay_raw.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExportError(f"{replay_file.name} invalid JSON: {exc}") from None
        if not isinstance(replay_doc, dict):
            raise ExportError(f"{replay_file.name} must be a JSON object")
        try:
            _require_exact_fields(replay_doc, TOP_LEVEL_FIELDS, f"{replay_file.name} top level")
        except ReplayError as exc:
            raise ExportError(str(exc)) from None
        if replay_doc.get("mode") != "REPLAY":
            raise ExportError(f"{replay_file.name} mode must be REPLAY")
        r_labels = replay_doc.get("labels")
        if not isinstance(r_labels, dict) or r_labels.get("mode") != "REPLAY" or r_labels.get("live") is not False:
            raise ExportError(f"{replay_file.name} labels must declare REPLAY and live=false")
        embedded_swarm_sha = extract_embedded_swarm_sha256(replay_doc, replay_file.name)
        if swarm_digest is not None and swarm_digest != embedded_swarm_sha:
            raise ExportError(
                f"{replay_id}.swarm.json sha256 ({swarm_digest}) does not match "
                f"{replay_file.name} provenance swarm_sha256 ({embedded_swarm_sha})"
            )

    if require_pin and swarm_digest is not None and swarm_pins is not None:
        if replay_id not in swarm_pins:
            raise ExportError(f"missing swarm bundle SHA-256 pin for {replay_id}")
        expected_swarm_pin = swarm_pins[replay_id].lower()
        if not SHA256_RE.fullmatch(expected_swarm_pin):
            raise ExportError(f"malformed swarm bundle SHA-256 pin for {replay_id}")
        if swarm_digest != expected_swarm_pin:
            raise ExportError(
                f"{replay_id}.swarm.json sha256 ({swarm_digest}) does not match pinned ({expected_swarm_pin})"
            )

    return data


def _validate_output_tree_containment(
    out_dir: Path,
    replay_id: str,
    staged_names: set[str] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Validate `out_dir` and all descendant targets for symlinks and canonical containment BEFORE any mutation."""
    out_lex_abs = Path(os.path.abspath(out_dir))
    probe: Path | None = out_lex_abs
    while probe is not None:
        try:
            if probe.is_symlink():
                raise ExportError(f"output directory or ancestor must not be a symlink: {probe}")
        except ExportError:
            raise
        except OSError as exc:
            raise ExportError(f"cannot stat output path ancestor {probe}: {exc}") from None
        parent = probe.parent
        if parent == probe:
            break
        probe = parent

    if out_lex_abs.exists() and not out_lex_abs.is_dir():
        raise ExportError(f"out_dir must be a directory: {out_dir}")

    out_resolved = out_lex_abs.resolve(strict=False)

    def _check_rel(rel_parts: tuple[str, ...], *, expect_dir: bool | None) -> Path:
        cur = out_lex_abs
        for part in rel_parts:
            if not part or part in (".", "..") or "/" in part or "\\" in part:
                raise ExportError(f"illegal output path segment {part!r} under {out_dir}")
            cur = cur / part
            try:
                if cur.is_symlink():
                    raise ExportError(f"output path component must not be a symlink: {cur}")
            except ExportError:
                raise
            except OSError as exc:
                raise ExportError(f"cannot stat output path component {cur}: {exc}") from None
            cur_resolved = cur.resolve(strict=False)
            if cur_resolved == out_resolved or not cur_resolved.is_relative_to(out_resolved):
                raise ExportError(
                    f"output path escapes out_dir ({out_resolved}): {cur} -> {cur_resolved}"
                )
        if expect_dir is True and cur.exists() and not cur.is_dir():
            raise ExportError(f"expected directory at output path, found non-directory: {cur}")
        if expect_dir is False and cur.exists() and cur.is_dir():
            raise ExportError(f"expected file at output path, found directory: {cur}")
        return cur

    for dir_parts in (
        ("replays",),
        ("static",),
        ("static", "data"),
        ("static", "frames"),
    ):
        _check_rel(dir_parts, expect_dir=True)

    # Guard against a planted symlink or rogue directory at `static/frames/<replay_id>` as well
    _check_rel(("static", "frames", replay_id), expect_dir=None)

    pid = os.getpid()
    file_targets: list[tuple[str, ...]] = [
        ("PINS.manifest",),
        (f".PINS.manifest.tmp.{pid}",),
        ("replays", "PINS.txt"),
        ("static", "data", "PINS.txt"),
        ("replays", f"{replay_id}.json"),
        ("replays", f".{replay_id}.json.tmp.{pid}"),
        ("static", "data", f"{replay_id}.swarm.json"),
        ("static", "data", f".{replay_id}.swarm.json.tmp.{pid}"),
    ]
    if staged_names:
        for fname in sorted(staged_names):
            file_targets.append(("static", "frames", fname))
            file_targets.append(("static", "frames", f".{fname}.tmp.{pid}"))

    for fparts in file_targets:
        _check_rel(fparts, expect_dir=False)

    frames_out = out_lex_abs / "static" / "frames"
    if frames_out.is_dir():
        for existing_png in sorted(frames_out.glob(f"{replay_id}_f*.png")):
            _check_rel(("static", "frames", existing_png.name), expect_dir=False)

    existing_r_pins: dict[str, str] = {}
    existing_s_pins: dict[str, str] = {}
    manifest_cand = out_lex_abs / "PINS.manifest"
    if manifest_cand.exists():
        existing_r_pins, existing_s_pins = load_pins_manifest(manifest_cand)

    return existing_r_pins, existing_s_pins


def export_replay(
    *,
    events_path: Path,
    frames_dir: Path,
    out_dir: Path,
    replay_id: str,
    sidecar_path: Path | None = None,
    min_ts_ms: int | None = None,
    max_frames: int = MAX_FRAMES,
    video: str | None = None,
    title: str | None = None,
    task: str | None = None,
) -> ExportResult:
    """Export validated `viewpoint.replay/v1` + `viewpoint.swarm_timeline/v1` bundles."""
    events_path = Path(events_path)
    frames_dir = Path(frames_dir)
    out_dir = Path(out_dir)

    if not isinstance(replay_id, str) or not REPLAY_ID_RE.fullmatch(replay_id):
        raise ExportError(
            f"invalid replay_id {replay_id!r} (must match {REPLAY_ID_RE.pattern})"
        )

    if isinstance(max_frames, bool) or not isinstance(max_frames, int) or not 1 <= max_frames <= MAX_FRAMES:
        raise ExportError(f"max_frames must be an integer in 1..{MAX_FRAMES}, got {max_frames!r}")

    _validate_output_tree_containment(out_dir, replay_id, staged_names=None)

    if frames_dir.is_symlink() or not frames_dir.is_dir():
        raise ExportError(f"frames_dir must be a real directory: {frames_dir}")

    video_url = _normalize_video_url(video)
    cutoff_ts_ms = resolve_cutoff_ts_ms(sidecar_path=sidecar_path, min_ts_ms=min_ts_ms)
    all_records = parse_events_jsonl(events_path)
    source_events_sha256 = hashlib.sha256(events_path.read_bytes()).hexdigest()

    if cutoff_ts_ms is not None:
        kept_records = [rec for rec in all_records if int(rec["_ts_ms"]) >= cutoff_ts_ms]
    else:
        kept_records = list(all_records)

    if not kept_records:
        raise ExportError(
            f"no records remain in {events_path} after cutoff start_ts_ms={cutoff_ts_ms}"
        )

    ref_nums = _collect_referenced_frame_nums(kept_records)
    if not ref_nums:
        discovered: list[int] = []
        for meta_file in sorted(frames_dir.glob("frame_*.json")):
            m = re.fullmatch(r"frame_([0-9]+)\.json", meta_file.name)
            if m:
                discovered.append(int(m.group(1)))
        ref_nums = sorted(discovered)

    if not ref_nums:
        raise ExportError(f"no referenced frames found in {frames_dir}")

    loaded_frames = [_load_source_frame(frames_dir, fnum) for fnum in ref_nums]

    # Filter source frames by cutoff_ts_ms unconditionally (whether 1 or N source frames exist)
    # and fail closed whenever zero eligible frames remain.
    if cutoff_ts_ms is not None:
        in_window_source_frames = [f for f in loaded_frames if f.ts_ms >= cutoff_ts_ms]
        if not in_window_source_frames:
            raise ExportError(
                f"zero source frames in {frames_dir} satisfy cutoff start_ts_ms={cutoff_ts_ms}"
            )
    else:
        in_window_source_frames = list(loaded_frames)

    # Prioritize preserving frames tied to spill, refusal, or uncertain outcomes when sampling
    priority_fnums: set[int] = set()
    for rec in kept_records:
        if rec["rec"] == "close" and rec.get("verdict") in (SPILL_VERDICTS | REFUSED_VERDICTS | PENDING_VERDICTS):
            vf = rec.get("verify_frame_ref")
            if isinstance(vf, int) and not isinstance(vf, bool) and vf >= 0:
                priority_fnums.add(vf)

    selected_frames = _select_frames(
        loaded_frames,
        cutoff_ts_ms=cutoff_ts_ms,
        max_frames=max_frames,
        priority_frame_nums=frozenset(priority_fnums),
    )

    min_event_ts = min(int(r["_ts_ms"]) for r in kept_records)
    max_event_ts = max(int(r["_ts_ms"]) for r in kept_records)
    window_start = cutoff_ts_ms if cutoff_ts_ms is not None else min(min_event_ts, selected_frames[0].ts_ms)
    window_end = max(max_event_ts, selected_frames[-1].ts_ms, window_start)

    replays_dir = out_dir / "replays"
    static_dir = out_dir / "static"
    frames_out_dir = static_dir / "frames"
    data_out_dir = static_dir / "data"

    # Map closing events to frames for rich captions
    close_by_verify_frame: dict[int, dict[str, Any]] = {}
    enqueue_by_event_id: dict[str, dict[str, Any]] = {}
    for rec in kept_records:
        if rec["rec"] == "enqueue" and isinstance(rec.get("event_id"), str):
            enqueue_by_event_id[rec["event_id"]] = rec
        elif rec["rec"] == "close" and rec.get("verdict") != "PASS":
            vf = rec.get("verify_frame_ref")
            if isinstance(vf, int) and not isinstance(vf, bool):
                close_by_verify_frame[vf] = rec

    replay_frames: list[dict[str, Any]] = []
    swarm_frames: list[dict[str, Any]] = []
    staged_pngs: list[tuple[str, bytes]] = []
    staged_fname_by_sha: dict[str, str] = {}
    frame_id_by_num: dict[int, str] = {}
    frame_t_list: list[int] = []
    frame_id_list: list[str] = []

    last_f_t = 0
    for idx0, sf in enumerate(selected_frames):
        fid = f"f{idx0:02d}"
        # Flat per-replay collision-free filename; deduplicate byte-identical frames within the same replay
        fname = staged_fname_by_sha.get(sf.sha256)
        if fname is None:
            fname = f"{replay_id}_f{idx0:02d}.png"
            staged_fname_by_sha[sf.sha256] = fname
            staged_pngs.append((fname, sf.png_bytes))
        src = f"/static/frames/{fname}"
        rel_t = max(last_f_t, max(0, sf.ts_ms - window_start))
        last_f_t = rel_t

        closing = close_by_verify_frame.get(sf.frame_num)
        if closing is not None:
            ev_id = closing.get("event_id") or "turn"
            ag_id = closing.get("agent_id") or "agent"
            verd = str(closing.get("verdict") or "OBSERVED")
            spill_n = len(closing.get("spill_regions") or []) if isinstance(closing.get("spill_regions"), list) else 0
            spill_note = f" (spill_regions={spill_n})" if verd == "VERIFIED_WITH_SPILL" or spill_n > 0 else ""
            enq = enqueue_by_event_id.get(str(ev_id))
            exp_txt = f" — {enq['expect']}" if enq and enq.get("expect") else ""
            raw_caption = (
                f"Frame {sf.frame_num} (scene_epoch={sf.scene_epoch}, view_epoch={sf.view_epoch}): "
                f"{ag_id} {ev_id} -> {verd}{spill_note}{exp_txt}"
            )
        else:
            raw_caption = (
                f"Frame {sf.frame_num} (scene_epoch={sf.scene_epoch}, view_epoch={sf.view_epoch}, "
                f"changed_regions={sf.changed_region_count})"
            )
        caption = _bounded_text(raw_caption, fallback=f"Frame {sf.frame_num}", limit=400)

        replay_frames.append(
            {
                "frame_id": fid,
                "t_ms": rel_t,
                "src": src,
                "sha256": sf.sha256,
                "caption": caption,
                "url": f"swarm://{replay_id}/frame/{sf.frame_num}",
            }
        )
        swarm_frames.append(
            {
                "frame_id": fid,
                "source_frame": sf.frame_num,
                "ts_ms": sf.ts_ms,
                "t_ms": rel_t,
                "src": src,
                "sha256": sf.sha256,
                "width": sf.width,
                "height": sf.height,
                "view_epoch": sf.view_epoch,
                "scene_epoch": sf.scene_epoch,
                "changed_region_count": sf.changed_region_count,
                "caption": caption,
            }
        )
        frame_id_by_num[sf.frame_num] = fid
        frame_t_list.append(rel_t)
        frame_id_list.append(fid)

    # Map any unsampled source frame number to the closest selected frame
    selected_nums = [sf.frame_num for sf in selected_frames]
    for lf in loaded_frames:
        if lf.frame_num not in frame_id_by_num:
            pos = bisect.bisect_right(selected_nums, lf.frame_num) - 1
            if pos < 0:
                pos = 0
            frame_id_by_num[lf.frame_num] = frame_id_list[pos]

    swarm_claims: list[dict[str, Any]] = []
    swarm_turns: list[dict[str, Any]] = []
    swarm_cursors: list[dict[str, Any]] = []
    swarm_controls: list[dict[str, Any]] = []
    verdict_counts: dict[str, int] = {}

    open_turns_by_num: dict[int, dict[str, Any]] = {}
    closed_event_ids: set[str] = set()
    candidate_steps: list[tuple[int, int, int, dict[str, Any]]] = []
    # tuple: (priority_tier, rel_t_ms, order_idx, step_payload_without_step_id)
    # priority_tier: 0 = control / non-PASS close, 1 = enqueue, 2 = claim, 3 = PASS close

    for order_idx, rec in enumerate(kept_records):
        rtype = rec["rec"]
        rec_ts = int(rec["_ts_ms"])
        rel_t = max(0, rec_ts - window_start)

        if rtype == "claim":
            ag_id = _bounded_text(rec.get("agent_id"), fallback="system", limit=64)
            lease_id = _bounded_text(rec.get("lease_id"), fallback="lease", limit=128)
            lease_epoch = _require_nonbool_int_ge0(rec.get("lease_epoch", 1), "claim.lease_epoch")
            ttl_ms = _require_nonbool_int_ge0(rec.get("ttl_ms", 0), "claim.ttl_ms")
            issued_ts = _require_nonbool_int_ge0(rec.get("issued_ts_ms", rec_ts), "claim.issued_ts_ms")
            expires_ts = _require_nonbool_int_ge0(rec.get("expires_ts_ms", rec_ts), "claim.expires_ts_ms")
            allow_gui = bool(rec.get("allow_gui", False))
            prefixes = [_sanitize_host_paths(str(p)) for p in (rec.get("write_prefixes") or [])]
            swarm_claims.append(
                {
                    "agent_id": ag_id,
                    "lease_id": lease_id,
                    "lease_epoch": lease_epoch,
                    "ttl_ms": ttl_ms,
                    "issued_ts_ms": issued_ts,
                    "expires_ts_ms": expires_ts,
                    "t_ms": rel_t,
                    "allow_gui": allow_gui,
                    "write_prefixes": prefixes,
                }
            )
            fid = _resolve_frame_id_for_ref(None, rel_t, frame_id_by_num, frame_t_list, frame_id_list)
            candidate_steps.append(
                (
                    2,
                    rel_t,
                    order_idx,
                    {
                        "t_ms": rel_t,
                        "actor": "system",
                        "action": _bounded_text(
                            f"[{ag_id}] Grant lease {lease_id} (epoch={lease_epoch}, allow_gui={str(allow_gui).lower()})",
                            fallback=f"[{ag_id}] Grant lease",
                            limit=500,
                        ),
                        "target": _bounded_text(f"{ag_id}/lease", fallback="lease", limit=200),
                        "outcome": _bounded_text(
                            f"Lease active; write_prefixes={','.join(prefixes) or 'none'}",
                            fallback="Lease active",
                            limit=600,
                        ),
                        "status": "verified",
                        "frame_id": fid,
                    },
                )
            )

        elif rtype == "enqueue":
            ag_id = _bounded_text(rec.get("agent_id"), fallback="planner", limit=64)
            ev_id = _bounded_text(rec.get("event_id"), fallback=f"ev-{order_idx}", limit=120)
            lane = _bounded_text(rec.get("lane"), fallback="script", limit=32)
            kind = _bounded_text(rec.get("kind"), fallback="job", limit=32)
            bof = rec.get("based_on_frame")
            bof_int = bof if isinstance(bof, int) and not isinstance(bof, bool) and bof >= 0 else None
            expect_str = _bounded_optional_text(rec.get("expect"), limit=900)
            wset = [_sanitize_host_paths(str(w)) for w in (rec.get("write_set") or [])]
            fid = _resolve_frame_id_for_ref(bof_int, rel_t, frame_id_by_num, frame_t_list, frame_id_list)
            candidate_steps.append(
                (
                    1,
                    rel_t,
                    order_idx,
                    {
                        "t_ms": rel_t,
                        "actor": "planner",
                        "action": _bounded_text(
                            f"[{ag_id}] Enqueue {ev_id} ({lane}/{kind}): {expect_str or 'intent'}",
                            fallback=f"[{ag_id}] Enqueue {ev_id}",
                            limit=500,
                        ),
                        "target": _bounded_text(f"{ag_id}/{ev_id}", fallback=ag_id, limit=200),
                        "outcome": _bounded_text(
                            f"Queued based_on_frame={bof_int if bof_int is not None else 'n/a'} "
                            f"write_set={','.join(wset) if wset else 'gui-view'}",
                            fallback="Intent queued",
                            limit=600,
                        ),
                        "status": "observed",
                        "frame_id": fid,
                    },
                )
            )

        elif rtype == "turn":
            t_num = rec.get("turn")
            if isinstance(t_num, int) and not isinstance(t_num, bool):
                open_turns_by_num[t_num] = rec

        elif rtype == "close":
            t_num = rec.get("turn")
            t_num_int = t_num if isinstance(t_num, int) and not isinstance(t_num, bool) else None
            open_turn = open_turns_by_num.pop(t_num_int, None) if t_num_int is not None else None
            turn_epoch = (
                int(open_turn["turn_epoch"])
                if open_turn is not None
                and isinstance(open_turn.get("turn_epoch"), int)
                and not isinstance(open_turn.get("turn_epoch"), bool)
                else t_num_int
            )
            head_frame = (
                int(open_turn["head_frame"])
                if open_turn is not None
                and isinstance(open_turn.get("head_frame"), int)
                and not isinstance(open_turn.get("head_frame"), bool)
                else None
            )

            tick_val = rec.get("tick")
            tick_int = tick_val if isinstance(tick_val, int) and not isinstance(tick_val, bool) else 0
            ag_id = _bounded_text(rec.get("agent_id"), fallback="engine", limit=64)
            raw_ev_id = rec.get("event_id")
            ev_id_str = str(raw_ev_id) if isinstance(raw_ev_id, str) and raw_ev_id.strip() else None
            if ev_id_str is not None:
                closed_event_ids.add(ev_id_str)
            enq = enqueue_by_event_id.get(ev_id_str) if ev_id_str is not None else None

            lane_val = rec.get("lane") or (enq.get("lane") if enq else None)
            lane_str = str(lane_val) if isinstance(lane_val, str) and lane_val else None
            kind_val = enq.get("kind") if enq else None
            kind_str = str(kind_val) if isinstance(kind_val, str) and kind_val else None

            verdict = str(rec["verdict"])
            status = _verdict_to_replay_status(verdict)
            verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1

            disp_seq = rec.get("dispatch_capture_seq")
            disp_int = disp_seq if isinstance(disp_seq, int) and not isinstance(disp_seq, bool) else None
            ver_ref = rec.get("verify_frame_ref")
            ver_int = ver_ref if isinstance(ver_ref, int) and not isinstance(ver_ref, bool) else None
            bof_val = enq.get("based_on_frame") if enq else None
            bof_int = bof_val if isinstance(bof_val, int) and not isinstance(bof_val, bool) else None

            chosen_ref = ver_int if ver_int is not None else (disp_int if disp_int is not None else (head_frame if head_frame is not None else bof_int))
            fid = _resolve_frame_id_for_ref(chosen_ref, rel_t, frame_id_by_num, frame_t_list, frame_id_list)

            own_regs = rec.get("own_regions") if isinstance(rec.get("own_regions"), list) else []
            spill_regs = rec.get("spill_regions") if isinstance(rec.get("spill_regions"), list) else []
            spill_count = len(spill_regs)
            drops_val = rec.get("drops", 0)
            drops_int = drops_val if isinstance(drops_val, int) and not isinstance(drops_val, bool) and drops_val >= 0 else 0

            expect_str = _bounded_optional_text(enq.get("expect") if enq else "", limit=900)
            reason_str = _bounded_optional_text(rec.get("reason"), limit=900)
            wset = [_sanitize_host_paths(str(w)) for w in (enq.get("write_set") or [])] if enq else []

            swarm_turns.append(
                {
                    "seq": len(swarm_turns) + 1,
                    "turn": t_num_int,
                    "turn_epoch": turn_epoch,
                    "tick": tick_int,
                    "ts_ms": rec_ts,
                    "t_ms": rel_t,
                    "agent_id": ag_id,
                    "event_id": _sanitize_host_paths(ev_id_str) if ev_id_str is not None else None,
                    "lane": _sanitize_host_paths(lane_str) if lane_str is not None else None,
                    "kind": _sanitize_host_paths(kind_str) if kind_str is not None else None,
                    "verdict": verdict,
                    "status": status,
                    "rebased": bool(rec.get("rebased", False)),
                    "drops": drops_int,
                    "based_on_frame": bof_int,
                    "head_frame": head_frame,
                    "dispatch_capture_seq": disp_int,
                    "verify_frame_ref": ver_int,
                    "frame_id": fid,
                    "write_set": wset,
                    "expect": expect_str,
                    "reason": reason_str,
                    "own_region_count": len(own_regs),
                    "spill_region_count": spill_count,
                }
            )

            tier = 3 if verdict == "PASS" else 0
            ev_tag = ev_id_str or f"turn-{t_num_int if t_num_int is not None else order_idx}"
            reason_suffix = f" ({reason_str})" if reason_str else ""
            if verdict == "VERIFIED_WITH_SPILL" or spill_count > 0:
                action_label = (
                    f"[{ag_id}] Turn {t_num_int if t_num_int is not None else '?'} "
                    f"close {ev_tag} -> {verdict} (spill_regions={spill_count}, "
                    f"lane={lane_str or 'ring'}, tick={tick_int})"
                )
                outcome_label = (
                    f"{verdict} (spill_regions={spill_count}) on {ev_tag} "
                    f"(agent={ag_id}, verify_frame={ver_int}){reason_suffix}"
                )
            else:
                action_label = (
                    f"[{ag_id}] Turn {t_num_int if t_num_int is not None else '?'} "
                    f"close {ev_tag} (lane={lane_str or 'ring'}, tick={tick_int})"
                )
                outcome_label = (
                    f"{verdict} on {ev_tag} (agent={ag_id}, verify_frame={ver_int}){reason_suffix}"
                )

            candidate_steps.append(
                (
                    tier,
                    rel_t,
                    order_idx,
                    {
                        "t_ms": rel_t,
                        "actor": "engine",
                        "action": _bounded_text(
                            action_label,
                            fallback=f"[{ag_id}] Close {ev_tag}",
                            limit=500,
                        ),
                        "target": _bounded_text(f"{ag_id}/{ev_tag}", fallback=ag_id, limit=200),
                        "outcome": _bounded_text(
                            outcome_label,
                            fallback=f"{verdict} on {ev_tag}",
                            limit=600,
                        ),
                        "status": status,
                        "frame_id": fid,
                    },
                )
            )

        elif rtype == "cursor":
            ag_id = _bounded_text(rec.get("agent_id"), fallback="camera-agent", limit=64)
            raw_ev = rec.get("event_id")
            ev_str = str(raw_ev) if isinstance(raw_ev, str) and raw_ev else None
            phase = str(rec.get("phase") or "act").lower()
            if phase not in {"intent", "act", "result"}:
                phase = "act"
            default_xy = CANONICAL_AGENT_PALETTE.get(ag_id, CANONICAL_AGENT_PALETTE["camera-agent"])["anchor_xy"]
            x_raw = rec.get("x", default_xy[0])
            y_raw = rec.get("y", default_xy[1])
            x_int = max(0, int(round(float(x_raw)))) if isinstance(x_raw, (int, float)) and not isinstance(x_raw, bool) else default_xy[0]
            y_int = max(0, int(round(float(y_raw)))) if isinstance(y_raw, (int, float)) and not isinstance(y_raw, bool) else default_xy[1]
            f_raw = rec.get("frame", 0)
            f_int = f_raw if isinstance(f_raw, int) and not isinstance(f_raw, bool) and f_raw >= 0 else 0
            fid = _resolve_frame_id_for_ref(f_int, rel_t, frame_id_by_num, frame_t_list, frame_id_list)
            raw_color = str(rec.get("color") or "")
            canon_color = CANONICAL_AGENT_PALETTE.get(ag_id, CANONICAL_AGENT_PALETTE["camera-agent"])["color"]
            color_str = raw_color if HEX_COLOR_RE.fullmatch(raw_color) else canon_color
            label_str = _bounded_text(rec.get("label"), fallback=f"{ag_id}: {phase}", limit=300)

            swarm_cursors.append(
                {
                    "seq": len(swarm_cursors) + 1,
                    "ts_ms": rec_ts,
                    "t_ms": rel_t,
                    "agent_id": ag_id,
                    "event_id": _sanitize_host_paths(ev_str) if ev_str is not None else None,
                    "phase": phase,
                    "x": x_int,
                    "y": y_int,
                    "frame": f_int,
                    "frame_id": fid,
                    "label": label_str,
                    "color": color_str,
                }
            )

        elif rtype == "control":
            op_str = str(rec.get("op") or "stop")
            if op_str not in {"stop", "resume", "reconcile", "park", "checkpoint"}:
                raise ExportError(f"invalid control op={op_str!r}", byte_offset=rec.get("_byte_offset"))
            by_str = str(rec.get("by") or "broker")
            if by_str not in {"director-agent", "broker", "operator"}:
                raise ExportError(f"invalid control by={by_str!r}", byte_offset=rec.get("_byte_offset"))
            reason_str = _bounded_optional_text(rec.get("reason"), limit=550)
            swarm_controls.append(
                {
                    "seq": len(swarm_controls) + 1,
                    "ts_ms": rec_ts,
                    "t_ms": rel_t,
                    "op": op_str,
                    "by": by_str,
                    "reason": reason_str,
                }
            )
            actor = "operator" if by_str == "operator" else ("planner" if by_str == "director-agent" else "system")
            status = "verified" if op_str in {"checkpoint", "resume"} else "observed"
            fid = _resolve_frame_id_for_ref(None, rel_t, frame_id_by_num, frame_t_list, frame_id_list)
            candidate_steps.append(
                (
                    0,
                    rel_t,
                    order_idx,
                    {
                        "t_ms": rel_t,
                        "actor": actor,
                        "action": _bounded_text(
                            f"[{by_str}] Control op={op_str}",
                            fallback=f"Control {op_str}",
                            limit=500,
                        ),
                        "target": "capsule/control",
                        "outcome": _bounded_text(
                            f"Control {op_str} by {by_str}: {reason_str or 'recorded'}",
                            fallback=f"Control {op_str}",
                            limit=600,
                        ),
                        "status": status,
                        "frame_id": fid,
                    },
                )
            )

    for ev_id, enq in enqueue_by_event_id.items():
        if ev_id not in closed_event_ids:
            rec_ts = int(enq["_ts_ms"])
            rel_t = max(0, rec_ts - window_start)
            bof_val = enq.get("based_on_frame")
            bof_int = bof_val if isinstance(bof_val, int) and not isinstance(bof_val, bool) and bof_val >= 0 else None
            fid = _resolve_frame_id_for_ref(bof_int, rel_t, frame_id_by_num, frame_t_list, frame_id_list)
            verdict_counts["QUEUED"] = verdict_counts.get("QUEUED", 0) + 1
            swarm_turns.append(
                {
                    "seq": len(swarm_turns) + 1,
                    "turn": None,
                    "turn_epoch": None,
                    "tick": 0,
                    "ts_ms": rec_ts,
                    "t_ms": rel_t,
                    "agent_id": _bounded_text(enq.get("agent_id"), fallback="planner", limit=64),
                    "event_id": _sanitize_host_paths(ev_id),
                    "lane": _sanitize_host_paths(str(enq.get("lane") or "script")),
                    "kind": _sanitize_host_paths(str(enq.get("kind") or "job")),
                    "verdict": "QUEUED",
                    "status": "pending",
                    "rebased": False,
                    "drops": 0,
                    "based_on_frame": bof_int,
                    "head_frame": None,
                    "dispatch_capture_seq": None,
                    "verify_frame_ref": None,
                    "frame_id": fid,
                    "write_set": [_sanitize_host_paths(str(w)) for w in (enq.get("write_set") or [])],
                    "expect": _bounded_optional_text(enq.get("expect"), limit=900),
                    "reason": "",
                    "own_region_count": 0,
                    "spill_region_count": 0,
                }
            )

    if not swarm_turns:
        first_rec = kept_records[0]
        rel_t = max(0, int(first_rec["_ts_ms"]) - window_start)
        verdict_counts["PASS"] = verdict_counts.get("PASS", 0) + 1
        swarm_turns.append(
            {
                "seq": 1,
                "turn": 0,
                "turn_epoch": 0,
                "tick": 0,
                "ts_ms": int(first_rec["_ts_ms"]),
                "t_ms": rel_t,
                "agent_id": str(first_rec.get("agent_id") or "modeler-agent"),
                "event_id": None,
                "lane": None,
                "kind": None,
                "verdict": "PASS",
                "status": "observed",
                "rebased": False,
                "drops": 0,
                "based_on_frame": None,
                "head_frame": selected_frames[0].frame_num,
                "dispatch_capture_seq": None,
                "verify_frame_ref": None,
                "frame_id": replay_frames[0]["frame_id"],
                "write_set": [],
                "expect": "",
                "reason": "",
                "own_region_count": 0,
                "spill_region_count": 0,
            }
        )

    swarm_turns.sort(key=lambda t: (t["t_ms"], t["seq"]))
    for idx, t in enumerate(swarm_turns, start=1):
        t["seq"] = idx

    # Select 1..MAX_STEPS replay steps: exclude empty PASS closes whenever substantive steps exist.
    non_pass_steps = [c for c in candidate_steps if c[0] < 3]
    chosen_pool = non_pass_steps if non_pass_steps else candidate_steps
    if not chosen_pool:
        raise ExportError("events log produced zero replay steps")

    if len(chosen_pool) > MAX_STEPS:
        by_priority = sorted(chosen_pool, key=lambda item: (item[0], item[1], item[2]))
        retained = by_priority[:MAX_STEPS]
        retained.sort(key=lambda item: (item[1], item[2]))
    else:
        retained = sorted(chosen_pool, key=lambda item: (item[1], item[2]))

    replay_steps: list[dict[str, Any]] = []
    last_s_t = 0
    for idx, (_, _, _, payload) in enumerate(retained, start=1):
        step_t = max(last_s_t, int(payload["t_ms"]))
        last_s_t = step_t
        step_obj = dict(payload)
        step_obj["step_id"] = f"s{idx:02d}"
        step_obj["t_ms"] = step_t
        replay_steps.append(step_obj)

    pass_closes_in_window = sum(
        1 for r in kept_records if r.get("rec") == "close" and r.get("verdict") == "PASS"
    )
    compacted_pass_count = (
        pass_closes_in_window
        if non_pass_steps
        else max(0, pass_closes_in_window - len(replay_steps))
    )

    budget_disclosure = (
        f"Exported {len(replay_steps)} steps (from {len(kept_records)} source records in window; "
        f"{compacted_pass_count} idle PASS closes compacted) and {len(replay_frames)} frames "
        f"(from {len(in_window_source_frames)} source frames); source events.jsonl sha256={source_events_sha256}."
    )
    limitations_list = [_sanitize_host_paths(item) for item in (budget_disclosure, *DEFAULT_LIMITATIONS)]

    recorded_at = datetime.fromtimestamp(window_start / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    active_agents = sorted(
        {
            str(r.get("agent_id"))
            for r in kept_records
            if r.get("rec") in {"enqueue", "close"} and r.get("verdict") != "PASS" and r.get("agent_id")
        }
    )
    if not active_agents:
        active_agents = list(RING_ORDER)

    resolved_title = _bounded_text(
        title,
        fallback=f"ViewPoint Swarm Replay — {replay_id} ({', '.join(active_agents)})",
        limit=240,
    )

    enq_expects = [
        f"{r.get('agent_id')}: {r.get('expect')}"
        for r in kept_records
        if r.get("rec") == "enqueue" and r.get("expect")
    ]
    default_task = (
        "; ".join(enq_expects)
        if enq_expects
        else f"Recorded ViewPoint Swarm turn-queue session ({replay_id}) across {', '.join(active_agents)}."
    )
    resolved_task = _bounded_text(task, fallback=default_task, limit=2000)

    verdict_summary = ", ".join(
        f"{k}={verdict_counts[k]}" for k in sorted(verdict_counts)
    ) or "no closed verdicts"
    terminal_outcome = _bounded_text(
        f"Recorded ViewPoint Swarm session {replay_id} completed with {len(kept_records)} in-window "
        f"records, {len(swarm_turns)} turns, {len(replay_frames)} SHA-256 verified still frames, and "
        f"verdict breakdown: {verdict_summary}. Read-only REPLAY bundle (live=false).",
        fallback=f"Recorded ViewPoint Swarm session {replay_id} (live=false).",
        limit=1200,
    )

    capture_desc = (
        f"Sidecar cutoff start_ts_ms={cutoff_ts_ms}; {len(replay_frames)} SHA-256 verified PNG frames"
        if cutoff_ts_ms is not None
        else f"Full log window start_ts_ms={window_start}; {len(replay_frames)} SHA-256 verified PNG frames"
    )

    swarm_doc: dict[str, Any] = {
        "schema": SWARM_SCHEMA,
        "replay_id": replay_id,
        "version": "1.0.0",
        "mode": "REPLAY",
        "live": False,
        "sample": False,
        "title": resolved_title,
        "task": resolved_task,
        "recorded_at": recorded_at,
        "window_start_ts_ms": window_start,
        "window_end_ts_ms": window_end,
        "duration_ms": window_end - window_start,
        "source_events_sha256": source_events_sha256,
        "video_url": video_url,
        "labels": {
            "mode": "REPLAY",
            "live": False,
            "sample": False,
        },
        "agent_palette": json.loads(json.dumps(CANONICAL_AGENT_PALETTE)),
        "frames": swarm_frames,
        "claims": swarm_claims,
        "turns": swarm_turns,
        "cursors": swarm_cursors,
        "controls": swarm_controls,
        "verdict_counts": verdict_counts,
        "limitations": list(limitations_list),
    }

    # Pre-validate swarm schema in memory BEFORE creating/modifying any output files
    validate_swarm_bundle(
        swarm_doc,
        static_dir=None,
        replay_dir=None,
        require_pin=False,
    )

    swarm_bytes = (json.dumps(swarm_doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if len(swarm_bytes) > MAX_SWARM_BYTES:
        raise ExportError(f"serialized swarm bundle exceeds {MAX_SWARM_BYTES} bytes")
    swarm_sha256 = hashlib.sha256(swarm_bytes).hexdigest()

    note_text = (
        "Exported by swarm/viewer/export_replay.py into viewpoint.replay/v1 and "
        f"viewpoint.swarm_timeline/v1; swarm_sha256={swarm_sha256}"
    )
    if len(note_text) > 600:
        raise ExportError("provenance.note exceeds 600 characters")

    replay_doc: dict[str, Any] = {
        "schema": REPLAY_SCHEMA,
        "replay_id": replay_id,
        "version": "1.0.0",
        "mode": "REPLAY",
        "sample": False,
        "sample_notice": (
            "Recorded ViewPoint Swarm evidence bundle (sample=false, live=false). "
            "Read-only static replay exported from authoritative events.jsonl and SHA-256 verified frames."
        ),
        "title": resolved_title,
        "task": resolved_task,
        "recorded_at": recorded_at,
        "provenance": {
            "source": _bounded_text(
                f"Authoritative swarm log {events_path.name} (v=1, {len(kept_records)} records, sha256={source_events_sha256})",
                fallback="Authoritative swarm events.jsonl",
                limit=600,
            ),
            "capture": _bounded_text(capture_desc, fallback="Verified swarm frames", limit=600),
            "note": note_text,
        },
        "labels": {
            "mode": "REPLAY",
            "live": False,
            "sample": False,
        },
        "frames": replay_frames,
        "steps": replay_steps,
        "terminal_outcome": terminal_outcome,
        "limitations": list(limitations_list),
    }

    replay_bytes = (json.dumps(replay_doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if len(replay_bytes) > MAX_REPLAY_BYTES:
        raise ExportError(f"serialized replay exceeds {MAX_REPLAY_BYTES} bytes")
    replay_sha256 = hashlib.sha256(replay_bytes).hexdigest()

    staged_names = {fname for fname, _ in staged_pngs}
    existing_r_pins, existing_s_pins = _validate_output_tree_containment(
        out_dir,
        replay_id,
        staged_names=staged_names,
    )

    # Create output directories and write all artifacts atomically
    replays_dir.mkdir(parents=True, exist_ok=True)
    frames_out_dir.mkdir(parents=True, exist_ok=True)
    data_out_dir.mkdir(parents=True, exist_ok=True)

    # Remove any legacy self-attested PINS.txt inside mutable directories (R4)
    for legacy_pins in (replays_dir / "PINS.txt", data_out_dir / "PINS.txt"):
        if legacy_pins.is_symlink():
            raise ExportError(f"refusing to unlink symlink legacy pins: {legacy_pins}")
        if legacy_pins.exists():
            legacy_pins.unlink()

    for fname, png_data in staged_pngs:
        dest_png = frames_out_dir / fname
        if dest_png.is_symlink():
            raise ExportError(f"refusing to overwrite symlink in output frames: {dest_png}")
        tmp_png = frames_out_dir / f".{fname}.tmp.{os.getpid()}"
        tmp_png.write_bytes(png_data)
        os.replace(tmp_png, dest_png)

    # Prune stale per-replay frames from prior exports of the same replay_id
    for old_png in frames_out_dir.glob(f"{replay_id}_f*.png"):
        if old_png.is_symlink():
            raise ExportError(f"refusing to prune symlink in output frames: {old_png}")
        if old_png.name not in staged_names:
            old_png.unlink()

    swarm_path = data_out_dir / f"{replay_id}.swarm.json"
    if swarm_path.is_symlink():
        raise ExportError(f"refusing to overwrite symlink swarm file: {swarm_path}")
    tmp_swarm = data_out_dir / f".{replay_id}.swarm.json.tmp.{os.getpid()}"
    tmp_swarm.write_bytes(swarm_bytes)
    os.replace(tmp_swarm, swarm_path)

    replay_path = replays_dir / f"{replay_id}.json"
    if replay_path.is_symlink():
        raise ExportError(f"refusing to overwrite symlink replay file: {replay_path}")
    tmp_replay = replays_dir / f".{replay_id}.json.tmp.{os.getpid()}"
    tmp_replay.write_bytes(replay_bytes)
    os.replace(tmp_replay, replay_path)

    manifest_path = out_dir / "PINS.manifest"
    merged_replay_pins: dict[str, str] = {}
    merged_swarm_pins: dict[str, str] = {}
    for existing_id, existing_sha in existing_r_pins.items():
        if (replays_dir / f"{existing_id}.json").is_file():
            merged_replay_pins[existing_id] = existing_sha
    for existing_id, existing_sha in existing_s_pins.items():
        if (data_out_dir / f"{existing_id}.swarm.json").is_file():
            merged_swarm_pins[existing_id] = existing_sha
    merged_replay_pins[replay_id] = replay_sha256
    merged_swarm_pins[replay_id] = swarm_sha256
    _write_pins_manifest(manifest_path, merged_replay_pins, merged_swarm_pins)

    # Mandatory post-write verification through ReplayStore(require_pin=True) + validate_swarm_bundle(require_pin=True)
    loaded_pins, loaded_swarm_pins = load_pins_manifest(manifest_path)
    try:
        store = ReplayStore(
            replays_dir,
            static_dir,
            pins=loaded_pins,
            require_pin=True,
        )
        verified_replay = store.get(replay_id)
    except ReplayError as exc:
        raise ExportError(f"ReplayStore verification failed: {exc}") from None

    if verified_replay.attestation != "pinned" or verified_replay.sha256 != replay_sha256:
        raise ExportError("exported replay failed pinned attestation check")

    validate_swarm_bundle(
        swarm_path,
        static_dir=static_dir,
        replay_dir=replays_dir,
        pins=loaded_pins,
        swarm_pins=loaded_swarm_pins,
        require_pin=True,
    )

    return ExportResult(
        replay_id=replay_id,
        replay_path=replay_path,
        replay_sha256=replay_sha256,
        swarm_path=swarm_path,
        swarm_sha256=swarm_sha256,
        manifest_path=manifest_path,
        pins_path=manifest_path,
        swarm_pins_path=manifest_path,
        source_events_sha256=source_events_sha256,
        frame_count=len(replay_frames),
        step_count=len(replay_steps),
        turn_count=len(swarm_turns),
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export a fail-closed ViewPoint Swarm static replay bundle (H5_r1)."
    )
    parser.add_argument("--events", type=Path, required=True, help="Path to authoritative events.jsonl")
    parser.add_argument("--frames", type=Path, required=True, help="Path to frames/ directory")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=_THIS_DIR,
        help="Viewer root directory (default: swarm/viewer)",
    )
    parser.add_argument(
        "--replay-id",
        type=str,
        required=True,
        help="Replay identifier matching ^[a-z0-9][a-z0-9._-]{2,63}$",
    )
    parser.add_argument(
        "--sidecar",
        type=Path,
        default=None,
        help="Optional <video>.meta.json sidecar with integer start_ts_ms cutoff",
    )
    parser.add_argument(
        "--min-ts-ms",
        type=int,
        default=None,
        help="Optional explicit minimum integer ts_ms cutoff",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=MAX_FRAMES,
        help=f"Maximum exported frames per replay (1..{MAX_FRAMES}, default: {MAX_FRAMES})",
    )
    parser.add_argument(
        "--video",
        type=str,
        default=None,
        help="Optional video filename for /static/videos/<name>.mp4 reference",
    )
    parser.add_argument("--title", type=str, default=None, help="Optional human-readable replay title")
    parser.add_argument("--task", type=str, default=None, help="Optional task description")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        res = export_replay(
            events_path=args.events,
            frames_dir=args.frames,
            out_dir=args.out_dir,
            replay_id=args.replay_id,
            sidecar_path=args.sidecar,
            min_ts_ms=args.min_ts_ms,
            max_frames=args.max_frames,
            video=args.video,
            title=args.title,
            task=args.task,
        )
    except ExportError as exc:
        print(f"ExportError: {exc}", file=sys.stderr)
        return 2
    except ReplayError as exc:
        print(f"ReplayError: {exc}", file=sys.stderr)
        return 2

    summary = {
        "replay_id": res.replay_id,
        "replay_path": str(res.replay_path),
        "replay_sha256": res.replay_sha256,
        "swarm_path": str(res.swarm_path),
        "swarm_sha256": res.swarm_sha256,
        "manifest_path": str(res.manifest_path),
        "source_events_sha256": res.source_events_sha256,
        "frames": res.frame_count,
        "steps": res.step_count,
        "turns": res.turn_count,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
