"""Load and validate versioned ViewPoint replay documents.

The gateway serves recorded sessions only. This loader is deliberately
strict: unknown schema versions, missing frames, path escapes, reordered
timelines and frame-hash mismatches are rejected at startup instead of
being rendered as evidence.

The public contract is documented in README.md. Keep this validator in
step with the schema; a field addition is a schema version change, not a
silent extension.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "viewpoint.replay/v1"
REPLAY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
ACTORS = {"operator", "engine", "planner", "system"}
STATUSES = {"verified", "refused", "pending", "observed"}
MAX_REPLAY_BYTES = 2_000_000
MAX_FRAMES = 40
MAX_STEPS = 60

TOP_LEVEL_FIELDS = {
    "schema",
    "replay_id",
    "version",
    "mode",
    "sample",
    "sample_notice",
    "title",
    "task",
    "recorded_at",
    "provenance",
    "labels",
    "frames",
    "steps",
    "terminal_outcome",
    "limitations",
}
FRAME_FIELDS = {"frame_id", "t_ms", "src", "sha256", "caption", "url"}
PROVENANCE_FIELDS = {"source", "capture", "note"}
LABEL_FIELDS = {"mode", "live", "sample"}
STEP_FIELDS = {
    "step_id",
    "t_ms",
    "actor",
    "action",
    "target",
    "outcome",
    "status",
    "frame_id",
}


class ReplayError(Exception):
    """A replay document cannot be admitted."""


class ReplayNotFound(ReplayError):
    """The requested replay id is not loaded."""


def parse_pins(text: str | None) -> dict[str, str]:
    """Parse REPLAY_PINS ("replay_id=sha256[,...]"), set outside replay/. Fail closed."""
    pins: dict[str, str] = {}
    for item in (text or "").split(","):
        item = item.strip()
        if not item:
            continue
        replay_id, sep, digest = item.partition("=")
        replay_id, digest = replay_id.strip(), digest.strip().lower()
        if not sep or not REPLAY_ID_RE.fullmatch(replay_id) or not SHA256_RE.fullmatch(digest):
            raise ReplayError("REPLAY_PINS entry is malformed")
        if replay_id in pins:
            raise ReplayError(f"REPLAY_PINS repeats {replay_id}")
        pins[replay_id] = digest
    return pins


@dataclass(frozen=True)
class Replay:
    replay_id: str
    data: dict[str, Any]
    sha256: str
    path: Path
    attestation: str = "self-attested"


def _require_text(value: Any, field: str, *, limit: int) -> str:
    if not isinstance(value, str):
        raise ReplayError(f"{field} must be a string")
    text = value.strip()
    if not text:
        raise ReplayError(f"{field} must not be empty")
    if len(text) > limit:
        raise ReplayError(f"{field} exceeds {limit} characters")
    return text


def _require_int(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReplayError(f"{field} must be an integer")
    if value < minimum:
        raise ReplayError(f"{field} must be >= {minimum}")
    return value


def _require_exact_fields(value: dict[str, Any], fields: set[str], where: str) -> None:
    missing = fields - set(value)
    unknown = set(value) - fields
    if missing:
        raise ReplayError(f"{where} is missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ReplayError(f"{where} has unknown fields: {', '.join(sorted(unknown))}")


class ReplayStore:
    """Immutable in-memory index of the replay/ directory."""

    def __init__(self, replay_dir: Path, static_dir: Path, *,
                 pins: Mapping[str, str] | None = None, require_pin: bool = False) -> None:
        self.replay_dir = Path(replay_dir)
        self.static_dir = Path(static_dir).resolve()
        self._by_id: dict[str, Replay] = {}
        self._order: list[str] = []
        self._pins = {str(k): str(v).lower() for k, v in (pins or {}).items()}
        for key, value in self._pins.items():
            if not REPLAY_ID_RE.fullmatch(key) or not SHA256_RE.fullmatch(value):
                raise ReplayError("pinned replay digest is malformed")
        self.require_pin = bool(require_pin)
        self._load()
        missing = sorted(set(self._pins) - set(self._by_id))
        if missing:
            raise ReplayError(f"pinned replay ids not loaded: {', '.join(missing)}")

    def _load(self) -> None:
        if not self.replay_dir.is_dir():
            raise ReplayError(f"replay directory not found: {self.replay_dir}")
        paths = sorted(self.replay_dir.glob("*.json"))
        if not paths:
            raise ReplayError(f"no replay documents in {self.replay_dir}")
        for path in paths:
            raw = path.read_bytes()
            if len(raw) > MAX_REPLAY_BYTES:
                raise ReplayError(f"{path.name} exceeds {MAX_REPLAY_BYTES} bytes")
            try:
                data = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ReplayError(f"{path.name} is not valid UTF-8 JSON: {exc}") from None
            if not isinstance(data, dict):
                raise ReplayError(f"{path.name} must contain a JSON object")
            self._validate(data, path)
            replay_id = data["replay_id"]
            if replay_id in self._by_id:
                raise ReplayError(f"duplicate replay_id: {replay_id}")
            digest = hashlib.sha256(raw).hexdigest()
            pinned = self._pins.get(replay_id)
            if pinned is not None:
                if pinned != digest:
                    raise ReplayError(f"{path.name} does not match its pinned sha256")
                attestation = "pinned"
            elif self.require_pin:
                raise ReplayError(f"{path.name} has no pinned sha256 (REPLAY_REQUIRE_PIN)")
            else:
                # Frame hashes, statuses and sample flags are checked only
                # against this same file: self-attested, not externally attested.
                attestation = "self-attested"
            self._by_id[replay_id] = Replay(
                replay_id=replay_id, data=data, sha256=digest, path=path,
                attestation=attestation,
            )
            self._order.append(replay_id)

    def _validate(self, data: dict[str, Any], path: Path) -> None:
        _require_exact_fields(data, TOP_LEVEL_FIELDS, f"{path.name} top level")

        if data["schema"] != SCHEMA:
            raise ReplayError(f"{path.name} has unsupported schema: {data['schema']!r}")
        replay_id = _require_text(data["replay_id"], "replay_id", limit=64)
        if not REPLAY_ID_RE.fullmatch(replay_id):
            raise ReplayError(f"{path.name} has an invalid replay_id")
        version = _require_text(data["version"], "version", limit=32)
        if not VERSION_RE.fullmatch(version):
            raise ReplayError(f"{path.name} has an invalid version")
        if data["mode"] != "REPLAY":
            raise ReplayError(f"{path.name} mode must be REPLAY")
        if not isinstance(data["sample"], bool):
            raise ReplayError(f"{path.name} sample must be a boolean")
        _require_text(data["sample_notice"], "sample_notice", limit=600)
        _require_text(data["title"], "title", limit=240)
        _require_text(data["task"], "task", limit=2000)
        _require_text(data["recorded_at"], "recorded_at", limit=64)
        _require_text(data["terminal_outcome"], "terminal_outcome", limit=1200)

        provenance = data["provenance"]
        if not isinstance(provenance, dict):
            raise ReplayError("provenance must be an object")
        _require_exact_fields(provenance, PROVENANCE_FIELDS, "provenance")
        for key in ("source", "capture", "note"):
            _require_text(provenance.get(key), f"provenance.{key}", limit=600)

        labels = data["labels"]
        if not isinstance(labels, dict):
            raise ReplayError("labels must be an object")
        _require_exact_fields(labels, LABEL_FIELDS, "labels")
        if labels.get("mode") != "REPLAY" or labels.get("live") is not False:
            raise ReplayError("labels must declare REPLAY and live=false")
        if labels.get("sample") is not data["sample"]:
            raise ReplayError("labels.sample must match sample")

        frames = data["frames"]
        if not isinstance(frames, list) or not 1 <= len(frames) <= MAX_FRAMES:
            raise ReplayError(f"frames must contain 1..{MAX_FRAMES} entries")
        frame_ids: set[str] = set()
        last_frame_t = -1
        for index, frame in enumerate(frames):
            where = f"frames[{index}]"
            if not isinstance(frame, dict):
                raise ReplayError(f"{where} must be an object")
            _require_exact_fields(frame, FRAME_FIELDS, where)
            frame_id = _require_text(frame["frame_id"], f"{where}.frame_id", limit=32)
            if frame_id in frame_ids:
                raise ReplayError(f"duplicate frame_id: {frame_id}")
            frame_ids.add(frame_id)
            timestamp = _require_int(frame["t_ms"], f"{where}.t_ms")
            if timestamp < last_frame_t:
                raise ReplayError("frames must be ordered by t_ms")
            last_frame_t = timestamp
            src = _require_text(frame["src"], f"{where}.src", limit=300)
            if not src.startswith("/static/frames/") or ".." in src:
                raise ReplayError(f"{where}.src must stay under /static/frames/")
            if not src.lower().endswith((".svg", ".png", ".jpg", ".jpeg", ".webp")):
                raise ReplayError(f"{where}.src must be a still image")
            digest = _require_text(frame["sha256"], f"{where}.sha256", limit=64)
            if not SHA256_RE.fullmatch(digest):
                raise ReplayError(f"{where}.sha256 must be lowercase hex")
            _require_text(frame["caption"], f"{where}.caption", limit=400)
            _require_text(frame["url"], f"{where}.url", limit=300)
            self._verify_frame(src, digest, where)

        steps = data["steps"]
        if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
            raise ReplayError(f"steps must contain 1..{MAX_STEPS} entries")
        step_ids: set[str] = set()
        last_step_t = -1
        for index, step in enumerate(steps):
            where = f"steps[{index}]"
            if not isinstance(step, dict):
                raise ReplayError(f"{where} must be an object")
            _require_exact_fields(step, STEP_FIELDS, where)
            step_id = _require_text(step["step_id"], f"{where}.step_id", limit=32)
            if step_id in step_ids:
                raise ReplayError(f"duplicate step_id: {step_id}")
            step_ids.add(step_id)
            timestamp = _require_int(step["t_ms"], f"{where}.t_ms")
            if timestamp < last_step_t:
                raise ReplayError("steps must be ordered by t_ms")
            last_step_t = timestamp
            if step["actor"] not in ACTORS:
                raise ReplayError(f"{where}.actor is not allowed")
            if step["status"] not in STATUSES:
                raise ReplayError(f"{where}.status is not allowed")
            _require_text(step["action"], f"{where}.action", limit=500)
            _require_text(step["target"], f"{where}.target", limit=200)
            _require_text(step["outcome"], f"{where}.outcome", limit=600)
            frame_id = _require_text(step["frame_id"], f"{where}.frame_id", limit=32)
            if frame_id not in frame_ids:
                raise ReplayError(f"{where}.frame_id does not exist")

        limitations = data["limitations"]
        if not isinstance(limitations, list) or len(limitations) > 20:
            raise ReplayError("limitations must be a list with at most 20 entries")
        for index, item in enumerate(limitations):
            _require_text(item, f"limitations[{index}]", limit=400)

    def _verify_frame(self, src: str, digest: str, where: str) -> None:
        relative = src.removeprefix("/static/")
        raw = self.static_dir / relative
        # Reject symlinks anywhere in the path: StaticAssets skips them,
        # so a symlinked frame would pass startup checks then 404 over HTTP.
        probe: Path | None = raw
        while probe is not None:
            try:
                if probe.is_symlink():
                    raise ReplayError(f"{where}.src must not be a symlink: {src}")
            except ReplayError:
                raise
            except OSError:
                break
            if probe == self.static_dir:
                break
            parent = probe.parent
            if parent == probe:
                break
            probe = parent
        candidate = raw.resolve()
        if not candidate.is_relative_to(self.static_dir):
            raise ReplayError(f"{where}.src escapes the static directory")
        if not candidate.is_file():
            raise ReplayError(f"{where}.src does not exist: {src}")
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if actual != digest:
            raise ReplayError(f"{where}.sha256 does not match {src}")

    def get(self, replay_id: str) -> Replay:
        if not isinstance(replay_id, str) or not REPLAY_ID_RE.fullmatch(replay_id):
            raise ReplayNotFound("unknown replay")
        try:
            return self._by_id[replay_id]
        except KeyError:
            raise ReplayNotFound("unknown replay") from None

    def all(self) -> list[Replay]:
        return [self._by_id[replay_id] for replay_id in self._order]

    def default_id(self) -> str:
        return self._order[0]

    @staticmethod
    def public_summary(replay: Replay) -> dict[str, Any]:
        data = replay.data
        return {
            "replay_id": replay.replay_id,
            "version": data["version"],
            "schema": data["schema"],
            "title": data["title"],
            "sample": data["sample"],
            "mode": data["mode"],
            "steps": len(data["steps"]),
            "frames": len(data["frames"]),
            "sha256": replay.sha256,
            "attestation": replay.attestation,
        }
