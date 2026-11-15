"""Blender 4.0.2 main-thread script bridge for ViewPoint Swarm (`vpswarm`).

Run inside Blender via:
    blender --factory-startup --python hackathon/swarm/broker/bridge.py

May also be imported in pure Python outside Blender for unit testing
(`discover_nonce`, `process_inbox_once`).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable

try:
    import bpy  # type: ignore[import-not-found]
except ImportError:
    bpy = None  # type: ignore[assignment]

DEFAULT_INBOX = "/dev/shm/vqa-cu/swarm/inbox"
DEFAULT_OUTBOX = "/dev/shm/vqa-cu/swarm/outbox"
DEFAULT_REJECTED = "/dev/shm/vqa-cu/swarm/rejected"
DEFAULT_NONCE_FILE = "/dev/shm/vqa-cu/swarm/nonce.txt"
POLL_INTERVAL_S = 0.2

JOB_FILENAME_RE = re.compile(r"\A([0-9a-fA-F]+)__(0|[1-9][0-9]*)__([A-Za-z0-9][A-Za-z0-9._-]{0,127})\.py\Z")
NONCE_RE = re.compile(r"\A[0-9a-fA-F]{8,64}\Z")
MAX_CAPTURED_OUTPUT_CHARS = 65536


def _now_ms() -> int:
    return int(time.time() * 1000)


def _get_paths(
    inbox_dir: str | Path | None = None,
    outbox_dir: str | Path | None = None,
    rejected_dir: str | Path | None = None,
    nonce_file: str | Path | None = None,
) -> tuple[Path, Path, Path, Path]:
    inbox = Path(inbox_dir if inbox_dir is not None else os.environ.get("SWARM_INBOX", DEFAULT_INBOX))
    outbox = Path(outbox_dir if outbox_dir is not None else os.environ.get("SWARM_OUTBOX", DEFAULT_OUTBOX))
    rejected = Path(
        rejected_dir if rejected_dir is not None else os.environ.get("SWARM_REJECTED", DEFAULT_REJECTED)
    )
    nfile = Path(nonce_file if nonce_file is not None else os.environ.get("SWARM_NONCE_FILE", DEFAULT_NONCE_FILE))
    return inbox, outbox, rejected, nfile


def discover_nonce(nonce_file: str | Path | None = None) -> str:
    """Discover the active run nonce from `SWARM_BRIDGE_NONCE` or `nonce.txt`."""
    env_nonce = os.environ.get("SWARM_BRIDGE_NONCE", "").strip()
    if env_nonce:
        if not NONCE_RE.fullmatch(env_nonce):
            raise ValueError(
                f"SWARM_BRIDGE_NONCE must be 8-64 hex characters (got {env_nonce!r}); "
                "use SWARM_NONCE_FILE for the nonce file path"
            )
        return env_nonce.lower()
    nfile = Path(nonce_file if nonce_file is not None else os.environ.get("SWARM_NONCE_FILE", DEFAULT_NONCE_FILE))
    try:
        if nfile.is_file():
            val = nfile.read_text(encoding="utf-8").strip()
            if val and NONCE_RE.fullmatch(val):
                return val.lower()
            return val
    except OSError:
        return ""
    return ""


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    tmp_path.write_text(data + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def _reject_file(src_path: Path, rejected_dir: Path, reason: str) -> None:
    rejected_dir.mkdir(parents=True, exist_ok=True)
    dest = rejected_dir / src_path.name
    try:
        os.replace(src_path, dest)
    except OSError:
        shutil.move(str(src_path), str(dest))
    log_entry = {
        "ts_ms": _now_ms(),
        "file": src_path.name,
        "reason": reason,
    }
    log_path = rejected_dir / "rejected.jsonl"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(log_entry, separators=(",", ":"), ensure_ascii=True) + "\n")


def _default_exec(source: str, job_path: Path) -> None:
    code = compile(source, str(job_path), "exec")
    ns: dict[str, Any] = {"__name__": "__main__", "__file__": str(job_path), "bpy": bpy}
    exec(code, ns)


def process_inbox_once(
    inbox_dir: str | Path | None = None,
    outbox_dir: str | Path | None = None,
    rejected_dir: str | Path | None = None,
    nonce_file: str | Path | None = None,
    exec_fn: Callable[[str, Path], Any] | None = None,
    max_jobs: int | None = None,
) -> dict[str, Any]:
    """Scan `SWARM_INBOX` once, reject invalid/mismatched files, and execute valid jobs."""
    inbox, outbox, rejected, nfile = _get_paths(inbox_dir, outbox_dir, rejected_dir, nonce_file)
    inbox.mkdir(parents=True, exist_ok=True)
    outbox.mkdir(parents=True, exist_ok=True)
    rejected.mkdir(parents=True, exist_ok=True)

    active_nonce = discover_nonce(nfile)
    runner = exec_fn if exec_fn is not None else _default_exec

    valid_jobs: list[tuple[int, str, Path, str, str]] = []
    rejected_files: list[str] = []

    try:
        entries = sorted(inbox.iterdir(), key=lambda p: p.name)
    except FileNotFoundError:
        entries = []

    for entry in entries:
        name = entry.name
        if name.startswith(".") or name.endswith(".tmp"):
            continue
        if not entry.is_file():
            continue

        match = JOB_FILENAME_RE.fullmatch(name)
        if not match:
            _reject_file(entry, rejected, "INVALID_NAME")
            rejected_files.append(name)
            continue

        file_nonce, seq_str, event_id = match.groups()
        if ".." in event_id:
            _reject_file(entry, rejected, "INVALID_NAME")
            rejected_files.append(name)
            continue
        if not active_nonce or file_nonce.lower() != active_nonce:
            _reject_file(entry, rejected, "NONCE_MISMATCH")
            rejected_files.append(name)
            continue

        valid_jobs.append((int(seq_str), name, entry, file_nonce.lower(), event_id))

    valid_jobs.sort(key=lambda item: (item[0], item[1]))
    if max_jobs is not None:
        valid_jobs = valid_jobs[:max_jobs]

    executed: list[dict[str, Any]] = []
    for seq, name, job_path, file_nonce, event_id in valid_jobs:
        if not job_path.exists():
            continue
        t_start = time.monotonic()
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        status = "OK"
        error_str: str | None = None

        try:
            source = job_path.read_text(encoding="utf-8")
            with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
                runner(source, job_path)
        except BaseException as exc:
            status = "ERROR"
            tb = traceback.format_exc()
            exc_name = type(exc).__name__
            error_str = tb if exc_name in tb else f"{exc_name}: {exc}\n{tb}"
        finally:
            elapsed_ms = int((time.monotonic() - t_start) * 1000)
            captured_out = stdout_buf.getvalue()
            captured_err = stderr_buf.getvalue()
            if captured_err:
                if captured_out and not captured_out.endswith("\n"):
                    captured_out += "\n"
                captured_out += f"[stderr]\n{captured_err}"
            if len(captured_out) > MAX_CAPTURED_OUTPUT_CHARS:
                captured_out = captured_out[:MAX_CAPTURED_OUTPUT_CHARS] + "\n...[truncated]"

            try:
                job_path.unlink()
            except FileNotFoundError:
                pass

            receipt = {
                "v": 1,
                "nonce": file_nonce,
                "seq": seq,
                "event_id": event_id,
                "status": status,
                "elapsed_ms": elapsed_ms,
                "stdout": captured_out,
                "error": error_str,
                "ts_ms": _now_ms(),
            }
            receipt_name = f"{file_nonce}__{seq}__{event_id}.json"
            _atomic_write_json(outbox / receipt_name, receipt)
            executed.append(receipt)

    return {
        "nonce": active_nonce,
        "executed": executed,
        "rejected": rejected_files,
    }


def _timer_tick() -> float:
    """Blender `bpy.app.timers` callback: processes one valid job per tick."""
    try:
        process_inbox_once(max_jobs=1)
    except BaseException:
        traceback.print_exc(file=sys.stderr)
    return POLL_INTERVAL_S


def register_timer() -> bool:
    """Register `_timer_tick` on Blender's main thread timer list."""
    if bpy is None:
        return False
    if not bpy.app.timers.is_registered(_timer_tick):
        bpy.app.timers.register(_timer_tick, first_interval=POLL_INTERVAL_S, persistent=True)
    return True


if __name__ == "__main__" and bpy is not None:
    register_timer()
