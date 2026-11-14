"""Turn-queue / actor broker for ViewPoint Swarm (`vpswarm`).

Implements `swarm/CONTRACT.md`:
- Per-run nonce generation (`nonce.txt`) and claim/lease table (`claim` records)
- Sole caller of `vision` (`look` and `act`), plus `--vision-mode mock` for offline tests
- Atomic frame publication (`frame_<N>.png`, `frame_<N>.json`, `latest.json`) + bounded frame ring
- Bounded per-agent intent FIFO ingestion (`max 8`, `QUEUE_FULL`, `DUPLICATE_EVENT`)
- Non-interactive deterministic round-robin turns (`modeler-agent`, `props-agent`,
  `look-agent`, `camera-agent`) and priority HI `director-agent` control boundaries
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PIL import Image, ImageDraw

VISION_BIN = "~/Documentos/vis-skill/vision"

DEFAULT_JOB = "vpswarm"
DEFAULT_INBOX = "/dev/shm/vqa-cu/swarm/inbox"
DEFAULT_OUTBOX = "/dev/shm/vqa-cu/swarm/outbox"
DEFAULT_REJECTED = "/dev/shm/vqa-cu/swarm/rejected"
DEFAULT_FRAMES_DIR = "/dev/shm/vqa-cu/swarm/frames"
DEFAULT_INTENTS_DIR = "/dev/shm/vqa-cu/swarm/intents"
DEFAULT_NONCE_FILE = "/dev/shm/vqa-cu/swarm/nonce.txt"
DEFAULT_EVENTS_LOG = "~/Documentos/viewpoint-hackathon/swarm/run/events.jsonl"

RING_AGENTS: tuple[str, ...] = (
    "modeler-agent",
    "props-agent",
    "look-agent",
    "camera-agent",
)
DIRECTOR_AGENT = "director-agent"
ALLOWED_GUI_KEYS: frozenset[str] = frozenset({"KP_1", "KP_3", "KP_7", "KP_4", "KP_Add", "Home"})
EVENT_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
NONCE_RE = re.compile(r"\A[0-9a-fA-F]{8,64}\Z")
VALID_CONTROL_OPS: frozenset[str] = frozenset({"stop", "resume", "reconcile", "park", "checkpoint"})
VALID_CONTROL_BYS: frozenset[str] = frozenset({"director-agent", "broker", "operator"})

MAX_FIFO = 8
MAX_STALE_DROPS = 3
CONSECUTIVE_TIMEOUT_PARK = 2

DEFAULT_LEASE_TTL_MS = 3_600_000
DEFAULT_FRAME_RING_SIZE = 64
DEFAULT_SCRIPT_TIMEOUT_S = 120.0
DEFAULT_GUI_TIMEOUT_S = 60.0
DEFAULT_POLL_MS = 200

FRAME_WIDTH = 1920
FRAME_HEIGHT = 1080
DIFF_BLOCK = 16

DEFAULT_CLAIMS: dict[str, dict[str, Any]] = {
    "modeler-agent": {"write_prefixes": ["col:CHAR_", "obj:CHAR_"], "allow_gui": False},
    "props-agent": {"write_prefixes": ["col:PROPS_", "obj:PROP_"], "allow_gui": False},
    "look-agent": {"write_prefixes": ["mat:", "light:", "world:"], "allow_gui": False},
    "camera-agent": {"write_prefixes": ["cam:", "render:"], "allow_gui": True},
}

CURSOR_SLOTS: dict[str, list[int]] = {
    "modeler-agent": [480, 420],
    "props-agent": [1080, 620],
    "look-agent": [780, 300],
    "camera-agent": [800, 500],
}

AGENT_COLORS: dict[str, str] = {
    "modeler-agent": "#f59e0b",
    "props-agent": "#10b981",
    "look-agent": "#a855f7",
    "camera-agent": "#38bdf8",
}


def now_ms() -> int:
    return int(time.time() * 1000)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n")


def atomic_copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dst.with_name(f".{dst.name}.tmp.{os.getpid()}")
    shutil.copyfile(src, tmp_path)
    os.replace(tmp_path, dst)


def normalize_regions(raw: Any) -> list[list[int]]:
    """Normalize `changed_regions` from dicts or `[x, y, w, h]` lists."""
    if not isinstance(raw, list):
        return []
    out: list[list[int]] = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) == 4:
            try:
                x, y, w, h = (int(item[0]), int(item[1]), int(item[2]), int(item[3]))
                if w > 0 and h > 0:
                    out.append([x, y, w, h])
            except (TypeError, ValueError):
                continue
        elif isinstance(item, dict):
            try:
                x = int(item["x"])
                y = int(item["y"])
                w = int(item["w"])
                h = int(item["h"])
                if w > 0 and h > 0:
                    out.append([x, y, w, h])
            except (KeyError, TypeError, ValueError):
                continue
    return out


def compute_changed_regions(prev_png: Path | None, curr_png: Path) -> list[list[int]]:
    """Compute deterministic bounding-box diff regions between two PNGs."""
    if prev_png is None or not prev_png.is_file() or not curr_png.is_file():
        return []
    with Image.open(prev_png) as im_prev, Image.open(curr_png) as im_curr:
        arr_prev = np.asarray(im_prev.convert("RGB"))
        arr_curr = np.asarray(im_curr.convert("RGB"))

    if arr_prev.shape != arr_curr.shape:
        h, w = arr_curr.shape[:2]
        return [[0, 0, int(w), int(h)]]

    diff_mask = np.any(arr_prev != arr_curr, axis=2)
    if not np.any(diff_mask):
        return []

    height, width = diff_mask.shape
    gh = (height + DIFF_BLOCK - 1) // DIFF_BLOCK
    gw = (width + DIFF_BLOCK - 1) // DIFF_BLOCK
    pad_h = gh * DIFF_BLOCK - height
    pad_w = gw * DIFF_BLOCK - width
    padded = np.pad(diff_mask, ((0, pad_h), (0, pad_w)), mode="constant", constant_values=False)
    grid = padded.reshape(gh, DIFF_BLOCK, gw, DIFF_BLOCK).any(axis=(1, 3))

    visited = np.zeros((gh, gw), dtype=bool)
    regions: list[list[int]] = []
    ys_blk, xs_blk = np.where(grid)

    for sy, sx in zip(ys_blk.tolist(), xs_blk.tolist()):
        if visited[sy, sx]:
            continue
        stack = [(sy, sx)]
        visited[sy, sx] = True
        min_x, max_x = width, -1
        min_y, max_y = height, -1

        while stack:
            by, bx = stack.pop()
            y0 = by * DIFF_BLOCK
            y1 = min((by + 1) * DIFF_BLOCK, height)
            x0 = bx * DIFF_BLOCK
            x1 = min((bx + 1) * DIFF_BLOCK, width)
            sub = diff_mask[y0:y1, x0:x1]
            ry, rx = np.where(sub)
            if len(ry) > 0:
                min_y = min(min_y, y0 + int(ry.min()))
                max_y = max(max_y, y0 + int(ry.max()))
                min_x = min(min_x, x0 + int(rx.min()))
                max_x = max(max_x, x0 + int(rx.max()))

            for ny in range(max(0, by - 1), min(gh, by + 2)):
                for nx in range(max(0, bx - 1), min(gw, bx + 2)):
                    if grid[ny, nx] and not visited[ny, nx]:
                        visited[ny, nx] = True
                        stack.append((ny, nx))

        if max_x >= min_x and max_y >= min_y:
            regions.append([min_x, min_y, max_x - min_x + 1, max_y - min_y + 1])

    regions.sort()
    return regions


def split_regions_by_footprint(
    regions: list[list[int]],
    footprint: list[int],
) -> tuple[list[list[int]], list[list[int]]]:
    """Partition `changed_regions` into `own_regions` (inside footprint) and `spill_regions`."""
    fx, fy, fw, fh = footprint
    fx2 = fx + fw
    fy2 = fy + fh
    own: list[list[int]] = []
    spill: list[list[int]] = []
    for r in regions:
        rx, ry, rw, rh = r
        if rx >= fx and ry >= fy and (rx + rw) <= fx2 and (ry + rh) <= fy2:
            own.append(r)
        else:
            spill.append(r)
    return own, spill


def _valid_footprint(fp: Any) -> bool:
    if not isinstance(fp, list) or len(fp) != 4:
        return False
    return all(isinstance(v, int) and not isinstance(v, bool) for v in fp) and fp[2] > 0 and fp[3] > 0


def _is_valid_event_id(event_id: Any) -> bool:
    if not isinstance(event_id, str):
        return False
    if "/" in event_id or "\\" in event_id or ".." in event_id:
        return False
    return EVENT_ID_RE.fullmatch(event_id) is not None


def _has_lone_surrogate(obj: Any) -> bool:
    """Return True if any string (or dict key/value) contains an unencodable UTF-16 lone surrogate."""
    if isinstance(obj, str):
        try:
            obj.encode("utf-8")
            return False
        except UnicodeEncodeError:
            return True
    if isinstance(obj, list):
        return any(_has_lone_surrogate(item) for item in obj)
    if isinstance(obj, dict):
        return any(_has_lone_surrogate(k) or _has_lone_surrogate(v) for k, v in obj.items())
    return False


def _safe_child_path(parent_dir: Path, filename: str) -> Path:
    if not filename or filename in (".", "..") or "/" in filename or "\\" in filename or ".." in filename:
        raise ValueError(f"Unsafe child filename: {filename!r}")
    resolved_parent = parent_dir.resolve()
    candidate = (resolved_parent / filename).resolve()
    if candidate.parent != resolved_parent:
        raise ValueError(f"Path traversal outside {resolved_parent}: {candidate}")
    return parent_dir / filename


def _safe_int_ts(val: Any, default: int = 0) -> int:
    if isinstance(val, bool) or val is None:
        return default
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        try:
            return int(val.strip())
        except ValueError:
            return default
    return default


@dataclass
class BrokerConfig:
    job: str = DEFAULT_JOB
    vision_mode: str = "live"
    inbox_dir: Path = field(default_factory=lambda: Path(os.environ.get("SWARM_INBOX", DEFAULT_INBOX)))
    outbox_dir: Path = field(default_factory=lambda: Path(os.environ.get("SWARM_OUTBOX", DEFAULT_OUTBOX)))
    rejected_dir: Path = field(default_factory=lambda: Path(os.environ.get("SWARM_REJECTED", DEFAULT_REJECTED)))
    frames_dir: Path = field(default_factory=lambda: Path(os.environ.get("SWARM_FRAMES_DIR", DEFAULT_FRAMES_DIR)))
    intents_dir: Path = field(default_factory=lambda: Path(os.environ.get("SWARM_INTENTS_DIR", DEFAULT_INTENTS_DIR)))
    events_log: Path = field(default_factory=lambda: Path(os.environ.get("SWARM_EVENTS_LOG", DEFAULT_EVENTS_LOG)))
    nonce_file: Path = field(default_factory=lambda: Path(os.environ.get("SWARM_NONCE_FILE", DEFAULT_NONCE_FILE)))
    lease_ttl_ms: int = DEFAULT_LEASE_TTL_MS
    frame_ring_size: int = DEFAULT_FRAME_RING_SIZE
    script_timeout_s: float = DEFAULT_SCRIPT_TIMEOUT_S
    gui_timeout_s: float = DEFAULT_GUI_TIMEOUT_S
    poll_ms: int = DEFAULT_POLL_MS
    max_ticks: int | None = None
    max_sweeps: int | None = None
    drain_and_exit: bool = False
    idle_timeout_s: float | None = None
    log_idle_pass: bool = False
    auto_rebase_viewport: bool = False
    auto_renew_leases: bool = True


class SwarmBroker:
    """Deterministic single-capsule turn-queue / actor broker."""

    def __init__(
        self,
        config: BrokerConfig,
        vision_runner: Callable[[list[str], float], subprocess.CompletedProcess[str]] | None = None,
        outbox_poll_hook: Callable[[], Any] | None = None,
    ) -> None:
        self.cfg = config
        self._vision_runner = vision_runner
        self._outbox_poll_hook = outbox_poll_hook
        self._stop_requested = False
        self._lock_fh: Any = None

        # Validate SWARM_BRIDGE_NONCE before acquiring resources
        env_nonce = os.environ.get("SWARM_BRIDGE_NONCE", "").strip()
        if env_nonce:
            if not NONCE_RE.fullmatch(env_nonce):
                raise ValueError(
                    f"SWARM_BRIDGE_NONCE must be 8-64 hex characters (got {env_nonce!r}); "
                    "use SWARM_NONCE_FILE for the nonce file path"
                )
            self.nonce = env_nonce.lower()
        else:
            self.nonce = secrets.token_hex(8)

        # Directories & exclusive broker lock
        self.cfg.events_log.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.cfg.events_log.with_name(self.cfg.events_log.name + ".lock")
        self._lock_fh = lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._lock_fh.close()
            self._lock_fh = None
            raise RuntimeError(f"Another broker holds exclusive lock on {lock_path}") from exc

        try:
            self.cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
            self.cfg.outbox_dir.mkdir(parents=True, exist_ok=True)
            self.cfg.rejected_dir.mkdir(parents=True, exist_ok=True)
            self.cfg.frames_dir.mkdir(parents=True, exist_ok=True)
            for agent_id in (*RING_AGENTS, DIRECTOR_AGENT):
                (self.cfg.intents_dir / agent_id).mkdir(parents=True, exist_ok=True)
                (self.cfg.intents_dir / agent_id / "processed").mkdir(parents=True, exist_ok=True)
                (self.cfg.intents_dir / agent_id / "rejected").mkdir(parents=True, exist_ok=True)

            atomic_write_text(self.cfg.nonce_file, self.nonce + "\n")

            # State machine & counters
            self.tick = 0
            self.turn = 0
            self.turn_epoch = 0
            self.view_epoch = 0
            self.scene_epoch = 0
            self.next_frame_seq = 0
            self.head_frame = 0
            self.frame_ring: deque[int] = deque(maxlen=max(1, self.cfg.frame_ring_size))
            self.prior_frame_png: Path | None = None

            self.fifos: dict[str, deque[dict[str, Any]]] = {a: deque() for a in RING_AGENTS}
            self.control_queue: deque[dict[str, Any]] = deque()
            self.seen_event_ids: set[str] = set()
            self.parked_agents: set[str] = set()
            self.consecutive_timeouts: dict[str, int] = {a: 0 for a in RING_AGENTS}
            self.actuator_busy: bool = False
            self._stale_drops: dict[str, int] = {}
            self._explicitly_expired_leases: set[str] = set()
            self.queue_state = "IDLE"
            self.ring_index = 0
            self.sweeps = 0
            self.last_activity = time.monotonic()

            # Issue initial leases
            self.leases: dict[str, dict[str, Any]] = {}
            self._issue_initial_leases()

            # Capture initial frame_0
            self.capture_frame()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """Release the exclusive broker lock file descriptor if held."""
        if self._lock_fh is not None:
            try:
                fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                self._lock_fh.close()
            except OSError:
                pass
            self._lock_fh = None

    def __del__(self) -> None:
        self.close()

    def request_stop(self) -> None:
        self._stop_requested = True

    def _emit_event(self, record: dict[str, Any]) -> None:
        payload = {"v": 1, **record}
        line = json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n"
        with self.cfg.events_log.open("a+b") as fh:
            fh.seek(0, os.SEEK_END)
            if fh.tell() > 0:
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    fh.seek(0, os.SEEK_END)
                    fh.write(b"\n")
            fh.seek(0, os.SEEK_END)
            fh.write(line.encode("ascii"))
            fh.flush()

    def _issue_initial_leases(self) -> None:
        issued = now_ms()
        expires = issued + self.cfg.lease_ttl_ms
        for agent_id in RING_AGENTS:
            spec = DEFAULT_CLAIMS[agent_id]
            short = agent_id.split("-")[0]
            claim = {
                "v": 1,
                "rec": "claim",
                "agent_id": agent_id,
                "lease_id": f"lease-{short}-0001",
                "lease_epoch": 1,
                "ttl_ms": self.cfg.lease_ttl_ms,
                "issued_ts_ms": issued,
                "expires_ts_ms": expires,
                "allow_gui": spec["allow_gui"],
                "write_prefixes": list(spec["write_prefixes"]),
            }
            self.leases[agent_id] = claim
            self._emit_event(claim)

    def _run_vision_cmd(self, args: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        cmd = ["python3", "-I", VISION_BIN, *args]
        if self._vision_runner is not None:
            return self._vision_runner(cmd, timeout_s)
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )

    def _render_mock_png(self, dst: Path, frame_num: int, mock_change_box: list[int] | None) -> None:
        img = Image.new("RGB", (FRAME_WIDTH, FRAME_HEIGHT), (30, 32, 38))
        draw = ImageDraw.Draw(img)
        if mock_change_box and _valid_footprint(mock_change_box):
            fx, fy, fw, fh = mock_change_box
            x0 = max(0, fx + min(10, fw // 4))
            y0 = max(0, fy + min(10, fh // 4))
            x1 = min(FRAME_WIDTH - 1, fx + fw - 1 - min(10, fw // 4))
            y1 = min(FRAME_HEIGHT - 1, fy + fh - 1 - min(10, fh // 4))
            if x1 > x0 and y1 > y0:
                draw.rectangle([x0, y0, x1, y1], fill=(56, 189, 248))
        elif frame_num > 0:
            x0 = 40 + ((frame_num * 23) % 400)
            y0 = 40 + ((frame_num * 17) % 200)
            draw.rectangle([x0, y0, x0 + 32, y0 + 32], fill=(245, 158, 11))
        tmp = dst.with_name(f".{dst.name}.tmp.{os.getpid()}")
        img.save(tmp, format="PNG")
        os.replace(tmp, dst)

    def capture_frame(
        self,
        *,
        mock_change_box: list[int] | None = None,
        override_regions: list[list[int]] | None = None,
    ) -> int:
        """Capture `frame_<N>`, publish sidecar and `latest.json`, and update frame ring."""
        frame_num = self.next_frame_seq
        self.next_frame_seq += 1

        frame_png = self.cfg.frames_dir / f"frame_{frame_num}.png"
        frame_json = self.cfg.frames_dir / f"frame_{frame_num}.json"
        latest_json = self.cfg.frames_dir / "latest.json"

        reported_regions: list[list[int]] = []
        width, height = FRAME_WIDTH, FRAME_HEIGHT

        if self.cfg.vision_mode == "mock":
            self._render_mock_png(frame_png, frame_num, mock_change_box)
        else:
            res = self._run_vision_cmd(["look", "--job", self.cfg.job], timeout_s=30.0)
            if res.returncode != 0:
                raise RuntimeError(f"vision look failed (rc={res.returncode}): {res.stderr.strip()}")
            payload = json.loads(res.stdout)
            src_str = payload.get("frame") or payload.get("png") or payload.get("screenshot")
            if not src_str or not Path(src_str).is_file():
                raise RuntimeError(f"vision look missing frame path: {payload}")
            atomic_copy_file(Path(src_str), frame_png)
            reported_regions = normalize_regions(payload.get("changed_regions"))
            with Image.open(frame_png) as im:
                width, height = im.size

        if override_regions is not None:
            changed_regions = override_regions
        elif reported_regions:
            changed_regions = reported_regions
        else:
            changed_regions = compute_changed_regions(self.prior_frame_png, frame_png)

        sidecar = {
            "v": 1,
            "frame": frame_num,
            "sha256": sha256_file(frame_png),
            "width": width,
            "height": height,
            "view_epoch": self.view_epoch,
            "scene_epoch": self.scene_epoch,
            "changed_regions": changed_regions,
            "ts_ms": now_ms(),
        }
        atomic_write_json(frame_json, sidecar)
        atomic_write_json(latest_json, sidecar)

        self.prior_frame_png = frame_png
        self.head_frame = frame_num
        self.frame_ring.append(frame_num)
        return frame_num

    def _emit_cursor(
        self,
        agent_id: str,
        event_id: str,
        phase: str,
        label: str,
        cursor_xy: Any = None,
    ) -> None:
        if (
            isinstance(cursor_xy, (list, tuple))
            and len(cursor_xy) == 2
            and all(isinstance(v, int) and not isinstance(v, bool) for v in cursor_xy)
        ):
            x, y = cursor_xy[0], cursor_xy[1]
        else:
            x, y = CURSOR_SLOTS.get(agent_id, [800, 500])
        self._emit_event(
            {
                "rec": "cursor",
                "agent_id": agent_id,
                "event_id": event_id,
                "phase": phase,
                "x": x,
                "y": y,
                "frame": self.head_frame,
                "label": label,
                "color": AGENT_COLORS.get(agent_id, "#94a3b8"),
                "ts_ms": now_ms(),
            }
        )

    def _emit_close(
        self,
        *,
        tick: int,
        turn: int,
        event_id: str | None,
        agent_id: str,
        lane: str | None,
        verdict: str,
        rebased: bool = False,
        dispatch_capture_seq: int | None = None,
        verify_frame_ref: int | None = None,
        own_regions: list[list[int]] | None = None,
        spill_regions: list[list[int]] | None = None,
        drops: int = 0,
        reason: str = "",
    ) -> None:
        self._emit_event(
            {
                "rec": "close",
                "tick": tick,
                "turn": turn,
                "event_id": event_id,
                "agent_id": agent_id,
                "lane": lane,
                "verdict": verdict,
                "rebased": rebased,
                "dispatch_capture_seq": dispatch_capture_seq,
                "verify_frame_ref": verify_frame_ref,
                "own_regions": own_regions if own_regions is not None else [],
                "spill_regions": spill_regions if spill_regions is not None else [],
                "drops": drops,
                "reason": reason,
                "ts_ms": now_ms(),
            }
        )

    def _archive_intent(self, path: Path, agent_id: str, subdir: str = "processed") -> None:
        dest_dir = self.cfg.intents_dir / agent_id / subdir
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        try:
            os.replace(path, dest)
        except OSError:
            shutil.move(str(path), str(dest))

    def ingest_intents(self) -> int:
        """Scan agent and director intent directories, archive consumed files, and enqueue."""
        ingested = 0

        # 1. Priority HI director-agent control files
        dir_path = self.cfg.intents_dir / DIRECTOR_AGENT
        if dir_path.is_dir():
            candidates: list[tuple[int, str, Path, dict[str, Any]]] = []
            for p in dir_path.iterdir():
                if not p.is_file() or p.name.startswith(".") or p.name.endswith(".tmp") or p.suffix != ".json":
                    continue
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        raise ValueError("director control payload must be a JSON object")
                    ts = _safe_int_ts(data.get("created_ts_ms", data.get("ts_ms", 0)), 0)
                except Exception as exc:
                    print(f"[broker] warning: archiving malformed director control {p.name}: {exc}", file=sys.stderr)
                    self._archive_intent(p, DIRECTOR_AGENT, subdir="rejected")
                    self.last_activity = time.monotonic()
                    continue
                candidates.append((ts, p.name, p, data))
            candidates.sort(key=lambda item: (item[0], item[1]))
            for _, _, p, data in candidates:
                self._archive_intent(p, DIRECTOR_AGENT, subdir="processed")
                self.control_queue.append(data)
                self.last_activity = time.monotonic()
                ingested += 1

        # 2. Planning agent intents
        for agent_id in RING_AGENTS:
            agent_dir = self.cfg.intents_dir / agent_id
            if not agent_dir.is_dir():
                continue
            loaded: list[tuple[int, str, Path, dict[str, Any]]] = []
            for p in agent_dir.iterdir():
                if not p.is_file() or p.name.startswith(".") or p.name.endswith(".tmp") or p.suffix != ".json":
                    continue
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        raise ValueError("intent payload must be a JSON object")
                    ts = _safe_int_ts(data.get("created_ts_ms"), 0)
                except Exception as exc:
                    print(f"[broker] warning: archiving malformed intent {p.name}: {exc}", file=sys.stderr)
                    self._archive_intent(p, agent_id, subdir="rejected")
                    self.last_activity = time.monotonic()
                    self._emit_close(
                        tick=self.tick,
                        turn=self.turn,
                        event_id=None,
                        agent_id=agent_id,
                        lane=None,
                        verdict="CLAIM_REFUSED",
                        reason="malformed intent JSON",
                    )
                    continue
                loaded.append((ts, p.name, p, data))

            loaded.sort(key=lambda item: (item[0], item[1]))
            for _, _, p, data in loaded:
                self.last_activity = time.monotonic()
                raw_lane = data.get("lane")
                lane_val = raw_lane if isinstance(raw_lane, str) and not _has_lone_surrogate(raw_lane) else None
                raw_event_id = data.get("event_id")

                if not _is_valid_event_id(raw_event_id):
                    self._archive_intent(p, agent_id, subdir="rejected")
                    self._emit_close(
                        tick=self.tick,
                        turn=self.turn,
                        event_id=None,
                        agent_id=agent_id,
                        lane=lane_val,
                        verdict="CLAIM_REFUSED",
                        reason="missing or invalid event_id",
                    )
                    continue

                if _has_lone_surrogate(data):
                    self._archive_intent(p, agent_id, subdir="rejected")
                    self._emit_close(
                        tick=self.tick,
                        turn=self.turn,
                        event_id=str(raw_event_id),
                        agent_id=agent_id,
                        lane=lane_val,
                        verdict="CLAIM_REFUSED",
                        reason="unencodable lone UTF-16 surrogate in intent",
                    )
                    continue

                self._archive_intent(p, agent_id, subdir="processed")
                event_id = str(raw_event_id)

                if event_id in self.seen_event_ids:
                    self._emit_close(
                        tick=self.tick,
                        turn=self.turn,
                        event_id=event_id,
                        agent_id=agent_id,
                        lane=lane_val,
                        verdict="DUPLICATE_EVENT",
                        reason="duplicate event_id",
                    )
                    continue

                if len(self.fifos[agent_id]) >= MAX_FIFO:
                    self._emit_close(
                        tick=self.tick,
                        turn=self.turn,
                        event_id=event_id,
                        agent_id=agent_id,
                        lane=lane_val,
                        verdict="QUEUE_FULL",
                        reason="fifo capacity 8 reached",
                    )
                    continue

                self.seen_event_ids.add(event_id)
                item = dict(data)
                item.pop("_drops", None)
                item.pop("drops", None)
                item.pop("rebased", None)
                item.pop("_rebased", None)
                item["_dir_agent_id"] = agent_id
                self._stale_drops[event_id] = 0
                self.fifos[agent_id].append(item)
                self._emit_enqueue_record(item, agent_id, event_id)
                self._emit_cursor(
                    agent_id=agent_id,
                    event_id=event_id,
                    phase="intent",
                    label=f"{agent_id}: enqueue {item.get('kind', 'intent')}",
                    cursor_xy=item.get("cursor_xy"),
                )
                ingested += 1

        return ingested

    def _emit_enqueue_record(self, item: dict[str, Any], agent_id: str, event_id: str) -> None:
        self._emit_event(
            {
                "rec": "enqueue",
                "event_id": event_id,
                "agent_id": agent_id,
                "lane": item.get("lane"),
                "action_class": item.get("action_class"),
                "kind": item.get("kind"),
                "based_on_frame": item.get("based_on_frame"),
                "view_epoch": item.get("view_epoch"),
                "scene_epoch": item.get("scene_epoch"),
                "lease_epoch": item.get("lease_epoch"),
                "write_set": item.get("write_set", []),
                "target_region": item.get("target_region"),
                "footprint": item.get("footprint"),
                "expect": item.get("expect", ""),
                "script_sha256": item.get("script_sha256"),
                "max_acts": item.get("max_acts", 1),
                "ts_ms": now_ms(),
            }
        )

    def apply_control_events(self) -> None:
        """Apply queued `director-agent` control events strictly at turn boundaries."""
        while self.control_queue:
            ctrl = self.control_queue.popleft()
            raw_op = ctrl.get("op")
            if not isinstance(raw_op, str) or raw_op.strip() not in VALID_CONTROL_OPS:
                print(f"[broker] warning: ignoring invalid control op={raw_op!r}", file=sys.stderr)
                continue
            op = raw_op.strip()

            raw_by = ctrl.get("by")
            if raw_by is None:
                by = DIRECTOR_AGENT
            elif isinstance(raw_by, str) and raw_by.strip() in VALID_CONTROL_BYS:
                by = raw_by.strip()
            else:
                print(f"[broker] warning: ignoring control with invalid by={raw_by!r}", file=sys.stderr)
                continue

            reason = str(ctrl.get("reason") or "")
            target_agent = ctrl.get("agent_id")
            target_event_id = ctrl.get("event_id")

            if op == "stop":
                self.queue_state = "FROZEN"
            elif op == "resume":
                was_frozen = self.queue_state == "FROZEN"
                self.queue_state = "IDLE"
                self.turn_epoch += 1
                if was_frozen:
                    self.view_epoch += 1
                    self.scene_epoch += 1
                if isinstance(target_agent, str) and target_agent in RING_AGENTS:
                    self.parked_agents.discard(target_agent)
                    self.consecutive_timeouts[target_agent] = 0
                elif target_agent is None:
                    self.parked_agents.clear()
                    for a in RING_AGENTS:
                        self.consecutive_timeouts[a] = 0
            elif op == "reconcile":
                self.actuator_busy = False
                if self.queue_state != "FROZEN":
                    self.queue_state = "IDLE"
                    self.turn_epoch += 1
                    try:
                        self.capture_frame()
                    except Exception as exc:
                        print(f"[broker] warning: reconcile capture_frame failed: {exc}", file=sys.stderr)
                        self.queue_state = "RECONCILE"
            elif op == "park":
                if isinstance(target_agent, str) and target_agent in RING_AGENTS:
                    self.parked_agents.add(target_agent)
            elif op == "checkpoint":
                self.actuator_busy = False
                if self.queue_state == "RECONCILE":
                    self.queue_state = "IDLE"
                    self.turn_epoch += 1

            record: dict[str, Any] = {
                "rec": "control",
                "op": op,
                "by": by,
                "reason": reason,
                "ts_ms": now_ms(),
            }
            if isinstance(target_agent, str) and target_agent in RING_AGENTS:
                record["agent_id"] = target_agent
            if _is_valid_event_id(target_event_id):
                record["event_id"] = target_event_id
            self._emit_event(record)

    def expire_lease(self, agent_id: str) -> None:
        """Explicitly expire an agent's lease for negative-control testing."""
        self._explicitly_expired_leases.add(agent_id)
        claim = self.leases.get(agent_id)
        if claim is not None:
            claim["expires_ts_ms"] = now_ms() - 1

    def renew_lease(self, agent_id: str) -> dict[str, Any] | None:
        """Renew an active unparked agent's lease while keeping `lease_epoch` unchanged."""
        claim = self.leases.get(agent_id)
        if claim is None or agent_id in self.parked_agents or agent_id in self._explicitly_expired_leases:
            return None
        issued = now_ms()
        claim["issued_ts_ms"] = issued
        claim["expires_ts_ms"] = issued + int(claim["ttl_ms"])
        self._emit_event(dict(claim))
        return claim

    def _maybe_renew_lease(self, agent_id: str) -> None:
        if not self.cfg.auto_renew_leases:
            return
        if agent_id in self.parked_agents or agent_id in self._explicitly_expired_leases:
            return
        claim = self.leases.get(agent_id)
        if claim is None:
            return
        ttl_ms = int(claim["ttl_ms"])
        renew_at = int(claim["expires_ts_ms"]) - (ttl_ms // 4)
        if now_ms() >= renew_at:
            self.renew_lease(agent_id)

    def _validate_lease(self, agent_id: str, intent: dict[str, Any]) -> str | None:
        claim = self.leases.get(agent_id)
        if claim is None:
            return "NO_LEASE"
        if intent.get("agent_id") != agent_id:
            return "CLAIM_REFUSED"
        if intent.get("lease_epoch") != claim["lease_epoch"]:
            return "LEASE_EXPIRED"
        self._maybe_renew_lease(agent_id)
        if now_ms() > claim["expires_ts_ms"]:
            return "LEASE_EXPIRED"
        return None

    def _handle_script_intent(self, agent_id: str, intent: dict[str, Any], claim: dict[str, Any]) -> None:
        event_id = str(intent["event_id"])
        write_set = intent.get("write_set")
        prefixes = claim["write_prefixes"]

        if (
            not isinstance(write_set, list)
            or len(write_set) == 0
            or not all(isinstance(w, str) and any(w.startswith(p) for p in prefixes) for w in write_set)
        ):
            self._emit_close(
                tick=self.tick,
                turn=self.turn,
                event_id=event_id,
                agent_id=agent_id,
                lane="script",
                verdict="CLAIM_REFUSED",
                reason="write_set outside active write_prefixes",
            )
            return

        script = intent.get("script")
        if not isinstance(script, str) or not script.strip():
            self._emit_close(
                tick=self.tick,
                turn=self.turn,
                event_id=event_id,
                agent_id=agent_id,
                lane="script",
                verdict="CLAIM_REFUSED",
                reason="empty script source",
            )
            return

        try:
            script_bytes = script.encode("utf-8")
        except UnicodeEncodeError:
            self._emit_close(
                tick=self.tick,
                turn=self.turn,
                event_id=event_id,
                agent_id=agent_id,
                lane="script",
                verdict="CLAIM_REFUSED",
                reason="script contains unencodable UTF-8 surrogates",
            )
            return

        expected_sha = intent.get("script_sha256")
        if expected_sha is not None:
            actual_sha = hashlib.sha256(script_bytes).hexdigest()
            if actual_sha != expected_sha:
                self._emit_close(
                    tick=self.tick,
                    turn=self.turn,
                    event_id=event_id,
                    agent_id=agent_id,
                    lane="script",
                    verdict="CLAIM_REFUSED",
                    reason="script_sha256 mismatch",
                )
                return

        self._emit_cursor(
            agent_id=agent_id,
            event_id=event_id,
            phase="act",
            label=f"{agent_id}: exec script",
            cursor_xy=intent.get("cursor_xy"),
        )

        dispatch_seq = self.tick + 1
        job_name = f"{self.nonce}__{dispatch_seq}__{event_id}.py"
        receipt_name = f"{self.nonce}__{dispatch_seq}__{event_id}.json"
        try:
            inbox_path = _safe_child_path(self.cfg.inbox_dir, job_name)
            outbox_path = _safe_child_path(self.cfg.outbox_dir, receipt_name)
        except ValueError as exc:
            self._emit_close(
                tick=self.tick,
                turn=self.turn,
                event_id=event_id,
                agent_id=agent_id,
                lane="script",
                verdict="CLAIM_REFUSED",
                reason=f"unsafe job path rejected: {exc}",
            )
            return

        dispatch_frame = self.head_frame
        atomic_write_text(inbox_path, script)

        deadline = time.monotonic() + self.cfg.script_timeout_s
        receipt: Any = None
        corrupt = False

        while True:
            if self._outbox_poll_hook is not None:
                self._outbox_poll_hook()
            if outbox_path.is_file():
                try:
                    receipt = json.loads(outbox_path.read_text(encoding="utf-8"))
                except Exception:
                    corrupt = True
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)

        if corrupt:
            self.tick += 1
            self.queue_state = "RECONCILE"
            self._emit_close(
                tick=self.tick,
                turn=self.turn,
                event_id=event_id,
                agent_id=agent_id,
                lane="script",
                verdict="OUTCOME_UNCERTAIN",
                dispatch_capture_seq=dispatch_frame,
                verify_frame_ref=None,
                reason="corrupt outbox receipt JSON",
            )
            return

        if receipt is None:
            # Do NOT unlink inbox_path: bridge may be opening or executing it on main thread.
            self.tick += 1
            self.turn_epoch += 1
            self.actuator_busy = True
            self.queue_state = "RECONCILE"
            self.consecutive_timeouts[agent_id] += 1
            self._emit_close(
                tick=self.tick,
                turn=self.turn,
                event_id=event_id,
                agent_id=agent_id,
                lane="script",
                verdict="TURN_TIMEOUT",
                dispatch_capture_seq=dispatch_frame,
                verify_frame_ref=None,
                reason=f"bridge receipt timeout after {self.cfg.script_timeout_s}s",
            )
            if self.consecutive_timeouts[agent_id] >= CONSECUTIVE_TIMEOUT_PARK:
                self.parked_agents.add(agent_id)
                self._emit_event(
                    {
                        "rec": "control",
                        "op": "park",
                        "by": "broker",
                        "agent_id": agent_id,
                        "event_id": event_id,
                        "reason": f"{agent_id}: 2 consecutive TURN_TIMEOUT",
                        "ts_ms": now_ms(),
                    }
                )
            return

        if not isinstance(receipt, dict):
            self.tick += 1
            self.queue_state = "RECONCILE"
            self._emit_close(
                tick=self.tick,
                turn=self.turn,
                event_id=event_id,
                agent_id=agent_id,
                lane="script",
                verdict="OUTCOME_UNCERTAIN",
                dispatch_capture_seq=dispatch_frame,
                verify_frame_ref=None,
                reason="outbox receipt JSON is not an object",
            )
            return

        status = receipt.get("status")
        if (
            receipt.get("nonce") != self.nonce
            or receipt.get("seq") != dispatch_seq
            or receipt.get("event_id") != event_id
            or status not in ("OK", "ERROR")
        ):
            self.tick += 1
            self.queue_state = "RECONCILE"
            self._emit_close(
                tick=self.tick,
                turn=self.turn,
                event_id=event_id,
                agent_id=agent_id,
                lane="script",
                verdict="OUTCOME_UNCERTAIN",
                dispatch_capture_seq=dispatch_frame,
                verify_frame_ref=None,
                reason="malformed or mismatched outbox receipt",
            )
            return

        self.consecutive_timeouts[agent_id] = 0
        self.tick += 1

        if status == "OK":
            self.scene_epoch += 1
            try:
                new_frame = self.capture_frame(mock_change_box=[*CURSOR_SLOTS.get(agent_id, [480, 420]), 40, 40])
            except Exception as exc:
                self.scene_epoch -= 1
                print(f"[broker] warning: post-script capture_frame failed: {exc}", file=sys.stderr)
                self.queue_state = "RECONCILE"
                self._emit_close(
                    tick=self.tick,
                    turn=self.turn,
                    event_id=event_id,
                    agent_id=agent_id,
                    lane="script",
                    verdict="OUTCOME_UNCERTAIN",
                    dispatch_capture_seq=dispatch_frame,
                    verify_frame_ref=None,
                    reason=f"post-dispatch capture_frame failed: {exc}",
                )
                return
            self._emit_close(
                tick=self.tick,
                turn=self.turn,
                event_id=event_id,
                agent_id=agent_id,
                lane="script",
                verdict="STATE_VERIFIED",
                dispatch_capture_seq=dispatch_frame,
                verify_frame_ref=new_frame,
            )
        else:
            try:
                new_frame = self.capture_frame()
            except Exception as exc:
                print(f"[broker] warning: post-error capture_frame failed: {exc}", file=sys.stderr)
                self.queue_state = "RECONCILE"
                self._emit_close(
                    tick=self.tick,
                    turn=self.turn,
                    event_id=event_id,
                    agent_id=agent_id,
                    lane="script",
                    verdict="OUTCOME_UNCERTAIN",
                    dispatch_capture_seq=dispatch_frame,
                    verify_frame_ref=None,
                    reason=f"post-error capture_frame failed: {exc}",
                )
                return
            err_msg = str(receipt.get("error") or "")[:300]
            self._emit_close(
                tick=self.tick,
                turn=self.turn,
                event_id=event_id,
                agent_id=agent_id,
                lane="script",
                verdict="JOB_ERROR",
                dispatch_capture_seq=dispatch_frame,
                verify_frame_ref=new_frame,
                reason=err_msg,
            )

    def _handle_gui_intent(self, agent_id: str, intent: dict[str, Any], claim: dict[str, Any]) -> None:
        event_id = str(intent["event_id"])
        if not claim.get("allow_gui", False):
            self._emit_close(
                tick=self.tick,
                turn=self.turn,
                event_id=event_id,
                agent_id=agent_id,
                lane="gui",
                verdict="CLAIM_REFUSED",
                reason="allow_gui is false for agent",
            )
            return

        footprint = intent.get("footprint")
        if not _valid_footprint(footprint):
            self._emit_close(
                tick=self.tick,
                turn=self.turn,
                event_id=event_id,
                agent_id=agent_id,
                lane="gui",
                verdict="FOOTPRINT_MISSING",
                reason="missing or invalid [x,y,w,h] footprint",
            )
            return

        keys = intent.get("keys")
        if (
            not isinstance(keys, list)
            or not (1 <= len(keys) <= 8)
            or not all(isinstance(k, str) and k in ALLOWED_GUI_KEYS for k in keys)
        ):
            self._emit_close(
                tick=self.tick,
                turn=self.turn,
                event_id=event_id,
                agent_id=agent_id,
                lane="gui",
                verdict="CLAIM_REFUSED",
                reason="keys missing, >8, or contains disallowed key",
            )
            return

        based_on_frame = intent.get("based_on_frame")
        if not isinstance(based_on_frame, int) or isinstance(based_on_frame, bool):
            self._emit_close(
                tick=self.tick,
                turn=self.turn,
                event_id=event_id,
                agent_id=agent_id,
                lane="gui",
                verdict="FRAME_EVICTED",
                reason="invalid based_on_frame",
            )
            return

        if based_on_frame not in self.frame_ring:
            self._emit_close(
                tick=self.tick,
                turn=self.turn,
                event_id=event_id,
                agent_id=agent_id,
                lane="gui",
                verdict="FRAME_EVICTED",
                reason=f"based_on_frame={based_on_frame} evicted from ring",
            )
            return

        rebased = False
        if based_on_frame < self.head_frame:
            if self.cfg.auto_rebase_viewport:
                rebased = True
            else:
                drops = self._stale_drops.get(event_id, 0) + 1
                self._stale_drops[event_id] = drops
                if drops < MAX_STALE_DROPS:
                    self.fifos[agent_id].append(intent)
                    self._emit_close(
                        tick=self.tick,
                        turn=self.turn,
                        event_id=event_id,
                        agent_id=agent_id,
                        lane="gui",
                        verdict="STALE_FRAME",
                        drops=drops,
                        reason=f"based_on_frame={based_on_frame} < head_frame={self.head_frame}",
                    )
                else:
                    self._emit_close(
                        tick=self.tick,
                        turn=self.turn,
                        event_id=event_id,
                        agent_id=agent_id,
                        lane="gui",
                        verdict="STALE_FRAME",
                        drops=drops,
                        reason="parked_after_3_stale_drops",
                    )
                    self._emit_event(
                        {
                            "rec": "control",
                            "op": "park",
                            "by": "broker",
                            "agent_id": agent_id,
                            "event_id": event_id,
                            "drops": drops,
                            "verdict": "STALE_FRAME",
                            "reason": f"parked_after_3_stale_drops:{event_id}",
                            "ts_ms": now_ms(),
                        }
                    )
                return

        self._emit_cursor(
            agent_id=agent_id,
            event_id=event_id,
            phase="act",
            label=f"{agent_id}: {','.join(keys)}",
            cursor_xy=intent.get("cursor_xy"),
        )

        dispatch_frame = self.head_frame
        deadline = time.monotonic() + self.cfg.gui_timeout_s
        act_regions: list[list[int]] = []
        any_verified_change = False
        uncertain_rc: int | None = None
        timed_out = False

        if self.cfg.vision_mode == "mock" and self._vision_runner is None:
            # Deterministic mock execution
            any_verified_change = True
        else:
            try:
                rem = max(0.05, deadline - time.monotonic())
                hover_res = self._run_vision_cmd(
                    ["act", "--job", self.cfg.job, "--xy", "800,500", "--expect-change", "none"],
                    timeout_s=rem,
                )
                if hover_res.returncode not in (0, 3):
                    uncertain_rc = hover_res.returncode
                else:
                    for key in keys:
                        rem = deadline - time.monotonic()
                        if rem <= 0:
                            timed_out = True
                            break
                        res = self._run_vision_cmd(
                            ["act", "--job", self.cfg.job, "--key", key, "--expect-change", "0,80,1580,900"],
                            timeout_s=rem,
                        )
                        if res.returncode == 0:
                            any_verified_change = True
                            try:
                                out_json = json.loads(res.stdout) if res.stdout.strip() else {}
                                rlist = normalize_regions(
                                    out_json.get("changed_regions")
                                    or out_json.get("receipt", {}).get("changed_regions")
                                )
                                act_regions.extend(rlist)
                            except json.JSONDecodeError:
                                pass
                        elif res.returncode == 3:
                            continue
                        else:
                            uncertain_rc = res.returncode
                            break
            except subprocess.TimeoutExpired:
                timed_out = True

        if timed_out:
            self.turn_epoch += 1
            self.tick += 1
            self.queue_state = "RECONCILE"
            self.consecutive_timeouts[agent_id] += 1
            verify_frame: int | None = None
            try:
                verify_frame = self.capture_frame()
            except Exception as exc:
                print(f"[broker] warning: post-gui-timeout capture_frame failed: {exc}", file=sys.stderr)
            self._emit_close(
                tick=self.tick,
                turn=self.turn,
                event_id=event_id,
                agent_id=agent_id,
                lane="gui",
                verdict="TURN_TIMEOUT",
                rebased=rebased,
                dispatch_capture_seq=dispatch_frame,
                verify_frame_ref=verify_frame,
                reason="gui_timeout_reconcile_required",
            )
            if self.consecutive_timeouts[agent_id] >= CONSECUTIVE_TIMEOUT_PARK:
                self.parked_agents.add(agent_id)
                self._emit_event(
                    {
                        "rec": "control",
                        "op": "park",
                        "by": "broker",
                        "agent_id": agent_id,
                        "event_id": event_id,
                        "reason": f"{agent_id}: 2 consecutive TURN_TIMEOUT",
                        "ts_ms": now_ms(),
                    }
                )
            return

        if uncertain_rc is not None:
            self.tick += 1
            self.queue_state = "RECONCILE"
            self._emit_close(
                tick=self.tick,
                turn=self.turn,
                event_id=event_id,
                agent_id=agent_id,
                lane="gui",
                verdict="INPUT_UNCERTAIN",
                rebased=rebased,
                dispatch_capture_seq=dispatch_frame,
                verify_frame_ref=None,
                reason=f"vision act exited rc={uncertain_rc}",
            )
            return

        if not any_verified_change:
            self.consecutive_timeouts[agent_id] = 0
            self.tick += 1
            try:
                new_frame = self.capture_frame()
            except Exception as exc:
                print(f"[broker] warning: post-gui-nochange capture_frame failed: {exc}", file=sys.stderr)
                self.queue_state = "RECONCILE"
                self._emit_close(
                    tick=self.tick,
                    turn=self.turn,
                    event_id=event_id,
                    agent_id=agent_id,
                    lane="gui",
                    verdict="INPUT_UNCERTAIN",
                    rebased=rebased,
                    dispatch_capture_seq=dispatch_frame,
                    verify_frame_ref=None,
                    reason=f"post-act capture_frame failed: {exc}",
                )
                return
            self._emit_close(
                tick=self.tick,
                turn=self.turn,
                event_id=event_id,
                agent_id=agent_id,
                lane="gui",
                verdict="NO_VISIBLE_CHANGE",
                rebased=rebased,
                dispatch_capture_seq=dispatch_frame,
                verify_frame_ref=new_frame,
            )
            return

        self.consecutive_timeouts[agent_id] = 0
        self.view_epoch += 1
        self.tick += 1
        try:
            new_frame = self.capture_frame(
                mock_change_box=footprint,
                override_regions=act_regions if act_regions else None,
            )
        except Exception as exc:
            self.view_epoch -= 1
            print(f"[broker] warning: post-gui capture_frame failed: {exc}", file=sys.stderr)
            self.queue_state = "RECONCILE"
            self._emit_close(
                tick=self.tick,
                turn=self.turn,
                event_id=event_id,
                agent_id=agent_id,
                lane="gui",
                verdict="INPUT_UNCERTAIN",
                rebased=rebased,
                dispatch_capture_seq=dispatch_frame,
                verify_frame_ref=None,
                reason=f"post-act capture_frame failed: {exc}",
            )
            return
        sidecar = json.loads((self.cfg.frames_dir / f"frame_{new_frame}.json").read_text(encoding="utf-8"))
        changed = normalize_regions(sidecar.get("changed_regions"))
        own_regions, spill_regions = split_regions_by_footprint(changed, footprint)
        verdict = "VERIFIED" if not spill_regions else "VERIFIED_WITH_SPILL"
        self._emit_close(
            tick=self.tick,
            turn=self.turn,
            event_id=event_id,
            agent_id=agent_id,
            lane="gui",
            verdict=verdict,
            rebased=rebased,
            dispatch_capture_seq=dispatch_frame,
            verify_frame_ref=new_frame,
            own_regions=own_regions,
            spill_regions=spill_regions,
        )

    def step_sweep(self) -> bool:
        """Run one deterministic round-robin ring sweep. Returns True if any work/turn was logged."""
        self.ingest_intents()
        self.apply_control_events()

        if self.queue_state != "IDLE":
            return False

        any_work = any(len(self.fifos[a]) > 0 for a in RING_AGENTS if a not in self.parked_agents)
        if not any_work and not self.cfg.log_idle_pass:
            return False

        self.sweeps += 1
        for _ in range(len(RING_AGENTS)):
            self.ingest_intents()
            self.apply_control_events()
            if self.queue_state != "IDLE":
                break
            if self.cfg.max_ticks is not None and self.tick >= self.cfg.max_ticks:
                break

            agent_id = RING_AGENTS[self.ring_index]
            self.ring_index = (self.ring_index + 1) % len(RING_AGENTS)

            if agent_id in self.parked_agents:
                continue

            self.turn += 1
            self.turn_epoch += 1

            fifo = self.fifos[agent_id]
            if not fifo:
                self._emit_event(
                    {
                        "rec": "turn",
                        "turn": self.turn,
                        "turn_epoch": self.turn_epoch,
                        "agent_id": agent_id,
                        "head_frame": self.head_frame,
                        "hold_ms": 0,
                        "ts_ms": now_ms(),
                    }
                )
                self._emit_close(
                    tick=self.tick,
                    turn=self.turn,
                    event_id=None,
                    agent_id=agent_id,
                    lane=None,
                    verdict="PASS",
                )
                continue

            intent = fifo.popleft()
            event_id = str(intent["event_id"])
            raw_lane = intent.get("lane")
            lane = raw_lane if isinstance(raw_lane, str) else ""
            hold_ms = int(
                (self.cfg.script_timeout_s if lane == "script" else self.cfg.gui_timeout_s) * 1000
            )
            self._emit_event(
                {
                    "rec": "turn",
                    "turn": self.turn,
                    "turn_epoch": self.turn_epoch,
                    "agent_id": agent_id,
                    "head_frame": self.head_frame,
                    "hold_ms": hold_ms,
                    "ts_ms": now_ms(),
                }
            )

            lease_err = self._validate_lease(agent_id, intent)
            if lease_err is not None:
                self._emit_close(
                    tick=self.tick,
                    turn=self.turn,
                    event_id=event_id,
                    agent_id=agent_id,
                    lane=lane or None,
                    verdict=lease_err,
                    reason=f"lease check failed: {lease_err}",
                )
            elif self.actuator_busy:
                self._emit_close(
                    tick=self.tick,
                    turn=self.turn,
                    event_id=event_id,
                    agent_id=agent_id,
                    lane=lane or None,
                    verdict="ACTUATOR_BUSY",
                    reason="actuator occupied by unreconciled job",
                )
            elif lane == "script":
                self._handle_script_intent(agent_id, intent, self.leases[agent_id])
            elif lane == "gui":
                self._handle_gui_intent(agent_id, intent, self.leases[agent_id])
            else:
                self._emit_close(
                    tick=self.tick,
                    turn=self.turn,
                    event_id=event_id,
                    agent_id=agent_id,
                    lane=lane or None,
                    verdict="CLAIM_REFUSED",
                    reason=f"unsupported lane: {lane}",
                )

            self._emit_cursor(
                agent_id=agent_id,
                event_id=event_id,
                phase="result",
                label=f"{agent_id}: done {event_id}",
                cursor_xy=intent.get("cursor_xy"),
            )
            self.last_activity = time.monotonic()

        return True

    def _has_pending_work(self) -> bool:
        if self.control_queue:
            return True
        return any(len(self.fifos[a]) > 0 for a in RING_AGENTS if a not in self.parked_agents)

    def run(self) -> int:
        """Main broker loop until stop, limit, drain, or idle timeout."""
        while not self._stop_requested:
            self.ingest_intents()
            self.apply_control_events()

            if self.cfg.max_ticks is not None and self.tick >= self.cfg.max_ticks:
                break
            if self.cfg.max_sweeps is not None and self.sweeps >= self.cfg.max_sweeps:
                break

            if self.queue_state != "IDLE":
                if self.cfg.drain_and_exit and not self.control_queue:
                    break
                if (
                    self.cfg.idle_timeout_s is not None
                    and not self.control_queue
                    and (time.monotonic() - self.last_activity) >= self.cfg.idle_timeout_s
                ):
                    return 2 if self.queue_state == "RECONCILE" else 0
                time.sleep(self.cfg.poll_ms / 1000.0)
                continue

            did_work = self.step_sweep()

            if self.cfg.max_ticks is not None and self.tick >= self.cfg.max_ticks:
                break
            if self.cfg.max_sweeps is not None and self.sweeps >= self.cfg.max_sweeps:
                break

            if self.cfg.drain_and_exit:
                self.ingest_intents()
                if not self._has_pending_work() or self.queue_state != "IDLE":
                    break

            if not did_work:
                if (
                    self.cfg.idle_timeout_s is not None
                    and not self._has_pending_work()
                    and (time.monotonic() - self.last_activity) >= self.cfg.idle_timeout_s
                ):
                    break
                time.sleep(self.cfg.poll_ms / 1000.0)

        return 0


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="ViewPoint Swarm Turn Queue / Actor Broker")
    p.add_argument("--job", default=DEFAULT_JOB)
    p.add_argument("--vision-mode", choices=("live", "mock"), default="live")
    p.add_argument("--inbox", default=os.environ.get("SWARM_INBOX", DEFAULT_INBOX))
    p.add_argument("--outbox", default=os.environ.get("SWARM_OUTBOX", DEFAULT_OUTBOX))
    p.add_argument("--rejected", default=os.environ.get("SWARM_REJECTED", DEFAULT_REJECTED))
    p.add_argument("--frames-dir", default=os.environ.get("SWARM_FRAMES_DIR", DEFAULT_FRAMES_DIR))
    p.add_argument("--intents-dir", default=os.environ.get("SWARM_INTENTS_DIR", DEFAULT_INTENTS_DIR))
    p.add_argument("--events-log", default=os.environ.get("SWARM_EVENTS_LOG", DEFAULT_EVENTS_LOG))
    p.add_argument("--nonce-file", default=os.environ.get("SWARM_NONCE_FILE", DEFAULT_NONCE_FILE))
    p.add_argument("--lease-ttl-ms", type=int, default=DEFAULT_LEASE_TTL_MS)
    p.add_argument("--frame-ring-size", type=int, default=DEFAULT_FRAME_RING_SIZE)
    p.add_argument("--script-timeout-s", type=float, default=DEFAULT_SCRIPT_TIMEOUT_S)
    p.add_argument("--gui-timeout-s", type=float, default=DEFAULT_GUI_TIMEOUT_S)
    p.add_argument("--poll-ms", type=int, default=DEFAULT_POLL_MS)
    p.add_argument("--max-ticks", type=int, default=None)
    p.add_argument("--max-sweeps", type=int, default=None)
    p.add_argument("--drain-and-exit", action="store_true")
    p.add_argument("--idle-timeout-s", type=float, default=None)
    p.add_argument("--log-idle-pass", action="store_true")
    p.add_argument("--auto-rebase-viewport", action="store_true")
    p.add_argument("--auto-renew-leases", action=argparse.BooleanOptionalAction, default=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    cfg = BrokerConfig(
        job=args.job,
        vision_mode=args.vision_mode,
        inbox_dir=Path(args.inbox),
        outbox_dir=Path(args.outbox),
        rejected_dir=Path(args.rejected),
        frames_dir=Path(args.frames_dir),
        intents_dir=Path(args.intents_dir),
        events_log=Path(args.events_log),
        nonce_file=Path(args.nonce_file),
        lease_ttl_ms=args.lease_ttl_ms,
        frame_ring_size=args.frame_ring_size,
        script_timeout_s=args.script_timeout_s,
        gui_timeout_s=args.gui_timeout_s,
        poll_ms=args.poll_ms,
        max_ticks=args.max_ticks,
        max_sweeps=args.max_sweeps,
        drain_and_exit=args.drain_and_exit,
        idle_timeout_s=args.idle_timeout_s,
        log_idle_pass=args.log_idle_pass,
        auto_rebase_viewport=args.auto_rebase_viewport,
        auto_renew_leases=args.auto_renew_leases,
    )
    broker = SwarmBroker(cfg)

    def _on_sig(signum: int, frame: Any) -> None:
        broker.request_stop()

    signal.signal(signal.SIGINT, _on_sig)
    signal.signal(signal.SIGTERM, _on_sig)
    try:
        return broker.run()
    finally:
        broker.close()


if __name__ == "__main__":
    raise SystemExit(main())
