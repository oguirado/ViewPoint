#!/usr/bin/env python3
"""Preflight a measured ARM64 capsule ramp and settle its recorded processes."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import select
import shutil
import signal
import stat
import time

if __package__:
    from . import arm64_capsule_session as m7
else:
    import arm64_capsule_session as m7


GIB_KIB = 1024 ** 2
MEMORY_FLOOR_KIB = 20 * GIB_KIB
# Reserve the entire M7 guardian MemoryMax for each planned session.
SESSION_RESERVATION_KIB = 8 * GIB_KIB
TERM_SECONDS = 8
KILL_SECONDS = 5
STATE_NAME = "ramp-state.json"
SCHEMA = "metaviewer-capsule-ramp/v1"


class Refused(ValueError):
    def __init__(self, code, detail, **fields):
        super().__init__(detail)
        self.code = code
        self.fields = fields


def require(condition, code, detail):
    if not condition:
        raise Refused(code, detail)


def mem_available(text):
    rows = [line for line in text.splitlines() if line.startswith("MemAvailable:")]
    match = re.fullmatch(r"MemAvailable:\s+([0-9]+) kB", rows[0]) if len(rows) == 1 else None
    require(match is not None, "memory_unknown", "MemAvailable must be present once in kB")
    return int(match[1])


def available_kib():
    return mem_available(Path("/proc/meminfo").read_text())


def admit(available, outstanding):
    require(all(type(value) is int and value >= 0 for value in (available, outstanding)),
            "memory_unknown", "invalid memory observation or reservation")
    peak = outstanding + SESSION_RESERVATION_KIB
    if available < MEMORY_FLOOR_KIB or available - peak < MEMORY_FLOOR_KIB:
        raise Refused("memory_floor", "available memory would breach the ramp floor",
                      mem_available_kib=available, floor_kib=MEMORY_FLOOR_KIB,
                      next_reservation_kib=SESSION_RESERVATION_KIB,
                      outstanding_reservation_kib=outstanding)
    return peak


def inode(info):
    return [info.st_dev, info.st_ino]


@contextmanager
def locked_root(path):
    path = Path(path)
    require(path.is_absolute() and path.resolve(strict=True) == path,
            "profile_root", "profile root must be an existing canonical absolute path")
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700
                and inode(info) == inode(path.lstat()),
                "profile_root", "owner-private profile root required")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        os.close(fd)


def empty_state(root_fd):
    return {"schema": SCHEMA, "stop_owner": os.getuid(),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "pid_namespace": inode(Path("/proc/self/ns/pid").stat()),
            "root_inode": inode(os.fstat(root_fd)), "sessions": [], "close_witnesses": []}


def read_state(root_fd, name=STATE_NAME):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600
                and 0 < info.st_size <= 1024 ** 2,
                "state", "owner-private regular state file required")
        raw = stream.read(1024 ** 2 + 1)
        require(len(raw) == info.st_size, "state", "state changed during read")
        state = json.loads(raw)
    expected = empty_state(root_fd)
    require(type(state) is dict and state.keys() == expected.keys(), "state", "invalid ramp state")
    require(all(state[key] == expected[key] for key in
                ("schema", "stop_owner", "boot_id", "pid_namespace", "root_inode")),
            "state", "state owner, boot, PID namespace or profile root differs")
    require(type(state["sessions"]) is list and type(state["close_witnesses"]) is list,
            "state", "invalid session or witness list")
    session_ids, pids = set(), set()
    for row in state["sessions"]:
        require(type(row) is dict and row.keys() ==
                {"session_id", "pid", "starttime", "pgid", "profile_inode"},
                "state", "invalid recorded session")
        ident, pid, profile = row["session_id"], row["pid"], row["profile_inode"]
        require(type(ident) is str and re.fullmatch(r"[0-9a-f]{32}", ident)
                and type(pid) is int and pid > 1 and pid != os.getpid()
                and type(row["starttime"]) is int and row["starttime"] > 0
                and type(profile) is list and len(profile) == 2
                and all(type(part) is int and part > 0 for part in profile)
                and ident not in session_ids and pid not in pids,
                "state", "invalid or duplicate session identity")
        require(type(row["pgid"]) is int and row["pgid"] > 1,
                "process_group", "recorded process group must exceed one")
        session_ids.add(ident)
        pids.add(pid)
    return state


def write_state(root_fd, state, name=STATE_NAME):
    temporary = ".ramp-" + secrets.token_hex(16)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=root_fd)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, allow_nan=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        os.fsync(root_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=root_fd)
        except FileNotFoundError:
            pass


def process_identity(pid):
    path = Path("/proc") / str(pid)
    try:
        require(path.stat().st_uid == os.getuid(), "process_identity", "foreign process owner")
        raw = (path / "stat").read_text(errors="surrogateescape")
    except FileNotFoundError:
        return None
    fields = raw.rpartition(")")[2].split()
    require(raw.startswith(f"{pid} (") and len(fields) >= 20,
            "process_identity", "invalid kernel process identity")
    return int(fields[19]), int(fields[2])


def wait_absent(row, timeout, poll):
    deadline = time.monotonic() + timeout
    while True:
        current = process_identity(row["pid"])
        if current is None or current[0] != row["starttime"]:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        # pidfd reports exit before the original parent has necessarily reaped.
        if poll.poll(max(1, min(50, int(remaining * 1000)))):
            time.sleep(min(0.01, remaining))


def stop(row):
    try:
        pidfd = os.pidfd_open(row["pid"])
    except ProcessLookupError:
        require(process_identity(row["pid"]) is None,
                "process_identity", "recorded PID changed while acquiring pidfd")
        return []
    try:
        poll = select.poll()
        poll.register(pidfd, select.POLLIN)
        sent = []
        for signum, timeout in ((signal.SIGTERM, TERM_SECONDS), (signal.SIGKILL, KILL_SECONDS)):
            current = process_identity(row["pid"])
            if current is None or (sent and current[0] != row["starttime"]):
                return sent
            require(current[1] > 1, "process_group", "observed process group must exceed one")
            require(current == (row["starttime"], row["pgid"]), "process_identity",
                    "recorded start time or process group differs")
            try:
                signal.pidfd_send_signal(pidfd, signum)
                sent.append(signum.name.removeprefix("SIG"))
            except ProcessLookupError:
                pass
            if wait_absent(row, timeout, poll):
                return sent
        raise Refused("process_residual", "recorded process remains after teardown", pid=row["pid"])
    finally:
        os.close(pidfd)


def check_profile(root_fd, name, expected):
    try:
        info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o700 and inode(info) == expected,
            "profile_identity", "recorded disposable profile changed")
    return True


def close_all(root_fd, name, state):
    while state["sessions"]:
        row = state["sessions"][0]
        profile = "session-" + row["session_id"]
        expected = row["profile_inode"]
        check_profile(root_fd, profile, expected)
        signals = stop(row)
        if check_profile(root_fd, profile, expected):
            require(shutil.rmtree.avoids_symlink_attacks, "profile_identity",
                    "descriptor-based tree removal required")
            shutil.rmtree(profile, dir_fd=root_fd)
        require(not check_profile(root_fd, profile, expected),
                "profile_residual", "recorded profile remains after teardown")
        state["close_witnesses"].append({
            "session_id": row["session_id"], "pid": row["pid"], "starttime": row["starttime"],
            "process_absent": True, "profile_removed": True, "signals": signals,
        })
        del state["sessions"][0]
        write_state(root_fd, state, name)
    return {"ok": True, "close_witnesses": state["close_witnesses"]}


def verify_scope(unit):
    require(re.fullmatch(r"[A-Za-z0-9_.@-]+\.scope", unit), "scope", "explicit scope unit required")
    record = Path("/proc/self/cgroup").read_text().strip()
    require(record.startswith("0::/") and "\n" not in record,
            "scope", "unified cgroup membership required")
    group = Path(record.removeprefix("0::"))
    require(group.name == unit and ".." not in group.parts,
            "scope", "caller is outside the requested scope")
    group = Path("/sys/fs/cgroup").joinpath(*group.parts[1:])
    for name, gib in (("memory.high", 6), ("memory.max", 8)):
        require((group / name).read_text().strip() == str(gib * GIB_KIB * 1024),
                "scope", f"effective {name} differs from the requested cap")


def open_preflight(args):
    require(args.n > 0, "count", "positive capsule count required")
    require(re.fullmatch(r":[0-9]+", args.display) and int(args.display[1:]) >= 90,
            "display", "private display :90 or higher required")
    verify_scope(args.scope_unit)
    with locked_root(Path(args.profile_root)):
        admit(available_kib(), 0)
        # The pinned M7 path has no compatible dispatch. Preserve its guardian,
        # protected registry fence, and graphical capsule admission.
        registry = m7.setup.registry_path()
        blockers = ["m7_guardian_outside_caller_scope", "m7_requires_graphical_add",
                    "m7_profile_root_fixed_to_dev_shm"]
        if args.n > 1:
            blockers.append("m7_single_held_session")
        if not registry.is_file():
            blockers.append("m7_registry_missing")
        raise Refused("m7_launch_unavailable", "pinned M7 cannot dispatch this ramp",
                      blockers=blockers, stop_owner=os.getuid())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    verbs = parser.add_subparsers(dest="verb", required=True)
    opening = verbs.add_parser("open")
    opening.add_argument("--n", type=int, required=True)
    opening.add_argument("--scope-unit", required=True)
    opening.add_argument("--profile-root", required=True)
    opening.add_argument("--display", required=True)
    closing = verbs.add_parser("close")
    closing.add_argument("--all-recorded", action="store_true", required=True)
    closing.add_argument("--state", required=True)
    verbs.add_parser("status").add_argument("--state", required=True)
    args = parser.parse_args(argv)
    try:
        if args.verb == "open":
            open_preflight(args)
        path = Path(args.state).absolute()
        with locked_root(path.parent) as root_fd:
            state = read_state(root_fd, path.name)
            result = close_all(root_fd, path.name, state) if args.verb == "close" else state
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, OSError) as error:
        reason = error.code if isinstance(error, Refused) else "unavailable"
        detail = str(error) if isinstance(error, Refused) else type(error).__name__
        fields = error.fields if isinstance(error, Refused) else {}
        print(json.dumps({"ok": False, "reason": reason, "detail": detail, **fields}, sort_keys=True))
        return 3 if args.verb == "open" else 2


if __name__ == "__main__":
    raise SystemExit(main())
