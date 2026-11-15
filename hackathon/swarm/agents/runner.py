"""Parallel headless agy agent launcher and PID lifecycle manager (H2)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from swarm.agents import (
    CANONICAL_ROLES,
    DEFAULT_BRIEFS_DIR,
    DEFAULT_PROOF_DIR,
    REPO_ROOT,
)
from swarm.agents.prompts import build_brief

AGY_BIN = "~/.local/bin/agy"
DEFAULT_GOMEMLIMIT = "6GiB"
DEFAULT_MODEL = "gemini-4-hydrogen-high"
DEFAULT_PRINT_TIMEOUT = "1800s"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def scope_unit_name(run_tag: str, role: str) -> str:
    return f"vpswarm-agent-{run_tag}-{role}"


def build_scope_prefix(unit_name: str) -> list[str]:
    """Build deterministic 6G/8G systemd-run user scope command prefix."""
    return [
        "systemd-run",
        "--user",
        "--scope",
        f"--unit={unit_name}",
        "-q",
        "-p",
        "MemoryHigh=6G",
        "-p",
        "MemoryMax=8G",
        "-p",
        "MemorySwapMax=0",
    ]


def build_agent_env(
    role: str,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Preserve shim GOMEMLIMIT and bind SWARM_AGENT_ID without invoking the 12G/16G shim."""
    env = dict(os.environ if base_env is None else base_env)
    env.setdefault("GOMEMLIMIT", DEFAULT_GOMEMLIMIT)
    env["SWARM_AGENT_ID"] = role
    return env


def build_agent_argv(
    *,
    role: str,
    model: str,
    cli_log_path: Path,
    print_timeout: str,
    brief: str,
    run_tag: str = "manual",
) -> list[str]:
    """Build exact systemd-run + real agy binary argv with -p as the very last flag."""
    unit_name = scope_unit_name(run_tag, role)
    return [
        *build_scope_prefix(unit_name),
        AGY_BIN,
        "--model",
        model,
        "--dangerously-skip-permissions",
        "--output-format",
        "stream-json",
        "--log-file",
        str(cli_log_path),
        "--print-timeout",
        print_timeout,
        "-p",
        brief,
    ]


def parse_roles(raw_roles: str) -> list[str]:
    roles = [r.strip() for r in raw_roles.split(",") if r.strip()]
    if not roles:
        raise ValueError("At least one role must be specified")
    unknown = [r for r in roles if r not in CANONICAL_ROLES]
    if unknown:
        raise ValueError(f"Unknown role(s): {unknown}; expected subset of {list(CANONICAL_ROLES)}")
    return roles


def read_proc_starttime_ticks(pid: int) -> int | None:
    """Read field 22 (starttime in clock ticks) from /proc/<pid>/stat."""
    stat_path = Path(f"/proc/{pid}/stat")
    if not stat_path.is_file():
        return None
    try:
        raw = stat_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    parts = raw.rsplit(")", 1)
    if len(parts) != 2:
        return None
    after = parts[1].split()
    if len(after) <= 19:
        return None
    try:
        return int(after[19])
    except ValueError:
        return None


def _is_pid_alive(pid: int) -> bool:
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.is_file():
        try:
            parts = stat_path.read_text(encoding="utf-8", errors="ignore").rsplit(")", 1)
            if len(parts) == 2 and parts[1].lstrip().startswith("Z"):
                return False
        except OSError:
            pass
    return True


def _stop_scope_unit(unit_name: str) -> None:
    if not unit_name.startswith("vpswarm-agent-"):
        return
    scope = unit_name if unit_name.endswith(".scope") else f"{unit_name}.scope"
    try:
        subprocess.run(
            ["systemctl", "--user", "stop", scope],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def kill_recorded_pids(proof_dir: Path, run_tag: str | None = None, grace_s: float = 2.0) -> list[dict[str, Any]]:
    """Terminate only processes whose PID and /proc/<pid>/stat starttime_ticks match .proc.json."""
    if not proof_dir.is_dir():
        return []
    proc_pattern = f"run_{run_tag}_*.proc.json" if run_tag else "run_*.proc.json"
    pid_pattern = f"run_{run_tag}_*.pid" if run_tag else "run_*.pid"

    by_prefix: dict[str, dict[str, Path | None]] = {}
    for pf in sorted(proof_dir.glob(proc_pattern)):
        stem = pf.name[: -len(".proc.json")]
        by_prefix.setdefault(stem, {"proc_file": None, "pid_file": None})["proc_file"] = pf
    for pf in sorted(proof_dir.glob(pid_pattern)):
        stem = pf.stem
        by_prefix.setdefault(stem, {"proc_file": None, "pid_file": None})["pid_file"] = pf

    outcomes: list[dict[str, Any]] = []
    targets: list[tuple[Path | None, Path | None, int, int, str]] = []

    for stem in sorted(by_prefix):
        proc_file = by_prefix[stem]["proc_file"]
        pid_file = by_prefix[stem]["pid_file"]
        if proc_file is None or not proc_file.is_file():
            outcomes.append(
                {
                    "pid_file": str(pid_file) if pid_file else None,
                    "status": "skipped_missing_proc_metadata",
                }
            )
            continue
        try:
            meta = json.loads(proc_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            outcomes.append({"proc_file": str(proc_file), "status": "invalid_proc_metadata"})
            continue

        pid = meta.get("pid")
        expected_ticks = meta.get("starttime_ticks")
        unit_name = str(meta.get("unit") or "")
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or not isinstance(expected_ticks, int)
            or isinstance(expected_ticks, bool)
        ):
            outcomes.append({"proc_file": str(proc_file), "status": "invalid_proc_metadata"})
            continue

        if pid <= 1 or pid == os.getpid():
            outcomes.append({"proc_file": str(proc_file), "pid": pid, "status": "skipped_unsafe_pid"})
            continue

        actual_ticks = read_proc_starttime_ticks(pid)
        if actual_ticks is None:
            _stop_scope_unit(unit_name)
            if pid_file is not None:
                pid_file.unlink(missing_ok=True)
            proc_file.unlink(missing_ok=True)
            outcomes.append({"proc_file": str(proc_file), "pid": pid, "status": "already_exited"})
            continue

        if actual_ticks != expected_ticks:
            outcomes.append(
                {
                    "proc_file": str(proc_file),
                    "pid": pid,
                    "expected_starttime_ticks": expected_ticks,
                    "actual_starttime_ticks": actual_ticks,
                    "status": "skipped_starttime_mismatch",
                }
            )
            continue

        targets.append((proc_file, pid_file, pid, expected_ticks, unit_name))

    for proc_file, pid_file, pid, expected_ticks, unit_name in targets:
        if unit_name:
            _stop_scope_unit(unit_name)
        if read_proc_starttime_ticks(pid) != expected_ticks or not _is_pid_alive(pid):
            if pid_file is not None:
                pid_file.unlink(missing_ok=True)
            proc_file.unlink(missing_ok=True)  # type: ignore[union-attr]
            outcomes.append(
                {"proc_file": str(proc_file), "pid": pid, "unit": unit_name, "status": "stopped_unit"}
            )
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            outcomes.append(
                {"proc_file": str(proc_file), "pid": pid, "unit": unit_name, "signal": "SIGTERM", "status": "signaled"}
            )
        except ProcessLookupError:
            outcomes.append({"proc_file": str(proc_file), "pid": pid, "status": "already_exited"})
        except PermissionError as exc:
            outcomes.append({"proc_file": str(proc_file), "pid": pid, "status": f"permission_error: {exc}"})

    if targets:
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline:
            if not any(
                _is_pid_alive(pid) and read_proc_starttime_ticks(pid) == expected_ticks
                for _, _, pid, expected_ticks, _ in targets
            ):
                break
            time.sleep(0.1)

        for proc_file, pid_file, pid, expected_ticks, _ in targets:
            if _is_pid_alive(pid) and read_proc_starttime_ticks(pid) == expected_ticks:
                try:
                    os.kill(pid, signal.SIGKILL)
                    outcomes.append(
                        {"proc_file": str(proc_file), "pid": pid, "signal": "SIGKILL", "status": "force_killed"}
                    )
                except ProcessLookupError:
                    pass
            if not _is_pid_alive(pid):
                if pid_file is not None:
                    pid_file.unlink(missing_ok=True)
                if proc_file is not None:
                    proc_file.unlink(missing_ok=True)

    return outcomes


def run_agents(
    *,
    roles: list[str],
    model: str = DEFAULT_MODEL,
    print_timeout: str = DEFAULT_PRINT_TIMEOUT,
    run_tag: str | None = None,
    proof_dir: Path = DEFAULT_PROOF_DIR,
    briefs_dir: Path = DEFAULT_BRIEFS_DIR,
    dry_run: bool = False,
) -> dict[str, Any]:
    tag = run_tag or datetime.now().strftime("%H%M%S")
    briefs_dir.mkdir(parents=True, exist_ok=True)
    proof_dir.mkdir(parents=True, exist_ok=True)

    planned: list[dict[str, Any]] = []
    for role in roles:
        brief = build_brief(role, run_id=f"run_{tag}")
        brief_path = briefs_dir / f"{role}.md"
        brief_path.write_text(brief, encoding="utf-8")

        prefix = f"run_{tag}_{role}"
        unit_name = scope_unit_name(tag, role)
        cli_log = proof_dir / f"{prefix}.cli.log"
        jsonl_log = proof_dir / f"{prefix}.jsonl"
        stderr_log = proof_dir / f"{prefix}.stderr.log"
        pid_file = proof_dir / f"{prefix}.pid"
        proc_file = proof_dir / f"{prefix}.proc.json"
        started_file = proof_dir / f"{prefix}.started_at.txt"
        finished_file = proof_dir / f"{prefix}.finished_at.txt"

        argv = build_agent_argv(
            role=role,
            model=model,
            cli_log_path=cli_log,
            print_timeout=print_timeout,
            brief=brief,
            run_tag=tag,
        )
        planned.append(
            {
                "role": role,
                "unit_name": unit_name,
                "brief_path": str(brief_path),
                "brief_sha256": hashlib.sha256(brief.encode("utf-8")).hexdigest(),
                "cli_log": cli_log,
                "jsonl_log": jsonl_log,
                "stderr_log": stderr_log,
                "pid_file": pid_file,
                "proc_file": proc_file,
                "started_file": started_file,
                "finished_file": finished_file,
                "argv": argv,
                "env": build_agent_env(role),
            }
        )

    if dry_run:
        return {
            "mode": "dry_run",
            "run_tag": tag,
            "model": model,
            "agents": [
                {
                    "role": item["role"],
                    "unit": item["unit_name"],
                    "brief_path": item["brief_path"],
                    "brief_sha256": item["brief_sha256"],
                    "argv_display": item["argv"][:-1] + [f"<brief:{len(item['argv'][-1])}chars>"],
                    "argv": item["argv"],
                }
                for item in planned
            ],
        }

    procs: list[tuple[dict[str, Any], subprocess.Popen[bytes], Any, Any]] = []
    for item in planned:
        started_ts = utc_now_iso()
        item["started_file"].write_text(started_ts + "\n", encoding="utf-8")
        item["started_at"] = started_ts

        out_fh = item["jsonl_log"].open("wb")
        err_fh = item["stderr_log"].open("wb")
        proc = subprocess.Popen(
            item["argv"],
            cwd=str(REPO_ROOT),
            env=item["env"],
            stdout=out_fh,
            stderr=err_fh,
        )
        starttime_ticks = read_proc_starttime_ticks(proc.pid)
        item["pid_file"].write_text(f"{proc.pid}\n", encoding="utf-8")
        item["proc_file"].write_text(
            json.dumps(
                {
                    "pid": proc.pid,
                    "starttime_ticks": starttime_ticks,
                    "unit": item["unit_name"],
                    "role": item["role"],
                    "run_tag": tag,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        item["pid"] = proc.pid
        procs.append((item, proc, out_fh, err_fh))

    summary_agents: list[dict[str, Any]] = []
    overall_rc = 0
    for item, proc, out_fh, err_fh in procs:
        rc = proc.wait()
        out_fh.close()
        err_fh.close()
        item["pid_file"].unlink(missing_ok=True)
        item["proc_file"].unlink(missing_ok=True)
        finished_ts = utc_now_iso()
        item["finished_file"].write_text(finished_ts + "\n", encoding="utf-8")
        if rc != 0 and overall_rc == 0:
            overall_rc = rc

        summary_agents.append(
            {
                "role": item["role"],
                "unit": item["unit_name"],
                "pid": item["pid"],
                "returncode": rc,
                "started_at": item["started_at"],
                "finished_at": finished_ts,
                "brief_path": item["brief_path"],
                "brief_sha256": item["brief_sha256"],
                "jsonl_log": str(item["jsonl_log"]),
                "jsonl_sha256": sha256_file(item["jsonl_log"]),
                "cli_log": str(item["cli_log"]),
                "cli_log_sha256": sha256_file(item["cli_log"]),
                "stderr_log": str(item["stderr_log"]),
                "stderr_sha256": sha256_file(item["stderr_log"]),
            }
        )

    summary = {
        "mode": "live",
        "run_tag": tag,
        "model": model,
        "overall_returncode": overall_rc,
        "agents": summary_agents,
    }
    summary_path = proof_dir / f"run_{tag}_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m swarm.agents.runner",
        description="Parallel headless agy agent runner inside systemd-run memory scopes.",
    )
    parser.add_argument(
        "--roles",
        default=",".join(CANONICAL_ROLES),
        help="Comma-separated roles to launch",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--print-timeout", default=DEFAULT_PRINT_TIMEOUT)
    parser.add_argument("--run-tag", default=None, help="Optional <HHMMSS> tag override")
    parser.add_argument("--proof-dir", default=str(DEFAULT_PROOF_DIR))
    parser.add_argument("--briefs-dir", default=str(DEFAULT_BRIEFS_DIR))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--kill-all", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    proof_dir = Path(args.proof_dir)
    briefs_dir = Path(args.briefs_dir)

    if args.kill_all:
        outcomes = kill_recorded_pids(proof_dir, run_tag=args.run_tag)
        print(json.dumps({"action": "kill_all", "outcomes": outcomes}, indent=2))
        return 0

    roles = parse_roles(args.roles)
    summary = run_agents(
        roles=roles,
        model=args.model,
        print_timeout=args.print_timeout,
        run_tag=args.run_tag,
        proof_dir=proof_dir,
        briefs_dir=briefs_dir,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        for item in summary["agents"]:
            print(f"[{item['role']}] brief={item['brief_path']} sha256={item['brief_sha256']}")
            print("  " + " ".join(item["argv_display"]))
        return 0

    print(json.dumps(summary, indent=2))
    return 0 if summary["overall_returncode"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
