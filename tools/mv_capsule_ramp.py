#!/usr/bin/env python3
"""Admit measured ARM64 capsule sessions and settle their recorded processes."""
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
import subprocess
import time

if __package__:
    from . import arm64_capsule_session as m7
else:
    import arm64_capsule_session as m7


GIB_KIB = 1024 ** 2
MEMORY_FLOOR_KIB = m7.MEMORY_FLOOR_KIB
# Reserve the entire M7 guardian MemoryMax for each planned session.
SESSION_RESERVATION_KIB = m7.SESSION_RESERVATION_KIB
TERM_SECONDS = 8
KILL_SECONDS = 5
M7_CLOSE_SECONDS = 30
READY_SECONDS = 60
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


def validate_session(row):
    keys = {"session_id", "pid", "starttime", "pgid", "profile_inode"}
    require(type(row) is dict and row.keys() in (keys, keys | {"custody"}),
            "state", "invalid recorded session")
    ident, pid, profile = row["session_id"], row["pid"], row["profile_inode"]
    id_pattern = r"[0-9a-f]{24}" if "custody" in row else r"[0-9a-f]{32}"
    require(type(ident) is str and re.fullmatch(id_pattern, ident)
            and type(pid) is int and pid > 1 and pid not in (os.getpid(), os.getpgid(0))
            and type(row["starttime"]) is int and row["starttime"] > 0
            and type(profile) is list and len(profile) == 2
            and all(type(part) is int and part > 0 for part in profile),
            "state", "invalid recorded session identity")
    require(type(row["pgid"]) is int and row["pgid"] > 1,
            "process_group", "recorded process group must exceed one")
    if "custody" in row:
        custody = row["custody"]
        m7.setup.closed(custody, ("registry", "registry_inode", "registry_sha256", "outer_cgroup"))
        require(row["pgid"] == pid and type(custody["registry"]) is str
                and type(custody["registry_inode"]) is list and len(custody["registry_inode"]) == 2
                and all(type(part) is int and part > 0 for part in custody["registry_inode"])
                and type(custody["registry_sha256"]) is str and re.fullmatch(r"[0-9a-f]{64}", custody["registry_sha256"])
                and type(custody["outer_cgroup"]) is str and custody["outer_cgroup"].startswith("/"),
                "state", "invalid M7 custody record")
    return ident, pid


def read_state(root_fd, name=STATE_NAME, *, allow_missing=False):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
    except FileNotFoundError:
        if allow_missing:
            return empty_state(root_fd)
        raise
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
        ident, pid = validate_session(row)
        require(ident not in session_ids and pid not in pids, "state", "duplicate session identity")
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


def stop(row, *, outer=None):
    phases = (((signal.SIGTERM, TERM_SECONDS), (signal.SIGKILL, KILL_SECONDS))
              if outer is None else ((signal.SIGTERM, M7_CLOSE_SECONDS),))
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
        for signum, timeout in phases:
            current = process_identity(row["pid"])
            if current is None or (sent and current[0] != row["starttime"]):
                return sent
            require(current[1] > 1, "process_group", "observed process group must exceed one")
            require(current == (row["starttime"], row["pgid"]), "process_identity",
                    "recorded start time or process group differs")
            if outer is not None:
                require(m7.process_alive(outer), "process_identity", "M7 outer identity changed")
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


def m7_slot(row):
    custody = row["custody"]
    registry = m7.setup.private_dir(custody["registry"])
    require(inode(registry.stat()) == custody["registry_inode"], "custody", "registry directory changed")
    return m7.multi_slot(registry, row["session_id"])


def m7_plan(slot, row):
    m7.setup.private_dir(slot)
    raw = m7.setup.read(slot / "reservation.json", private=True)
    marker = slot / "held-session.json"
    try:
        marker_raw = m7.setup.read(marker, private=True)
    except FileNotFoundError:
        require(not m7.present(marker), "custody", "held marker unavailable")
    else:
        require(marker_raw == raw, "custody", "held marker changed")
    held = m7.setup.decode(raw)
    m7.setup.closed(held, ("root", "unit", "status"))
    root = m7.private_ram_root(held["root"])
    plan = m7.setup.decode(m7.setup.read(root / "PLAN.json", private=True))
    custody = row["custody"]
    run_id = row["session_id"]
    require(type(plan) is dict and plan.get("schema") == "metaviewer-arm64-session/v1"
            and root.parent == m7.shm_base() and root.name.startswith(m7.session_root_prefix(run_id))
            and held["status"] == "closure-unproven"
            and held["unit"] == plan.get("unit") == "metaviewer-arm64-" + run_id + ".scope"
            and plan.get("run_id") == run_id and plan.get("multi_registry") == custody["registry"]
            and plan.get("registry_sha256") == custody["registry_sha256"],
            "custody", "session plan or reservation changed")
    outer = plan.get("outer")
    m7.setup.closed(outer, ("pid", "start_ticks", "cgroup", "state"))
    require((outer["pid"], outer["start_ticks"], outer["cgroup"]) ==
            (row["pid"], row["starttime"], custody["outer_cgroup"]),
            "process_identity", "foreign M7 outer process")
    return root, outer, held["unit"]


def close_m7(row):
    slot = m7_slot(row)
    marker = slot / "held-session.json"
    signals = []
    if m7.present(slot):
        root, outer, _ = m7_plan(slot, row)
        if m7.present(marker):
            signals = stop(row, outer=outer)
        proof = m7.multi_proof(slot)
        require(proof is not None, "custody", "M7 reconciliation proof required")
        m7_witness = {"root": str(root), "proof": str(proof)}
    else:
        m7_witness = {"disposition": "reservation_absent"}
    current = process_identity(row["pid"])
    require(current is None or current[0] != row["starttime"],
            "process_residual", "recorded M7 process remains")
    return signals, m7_witness


def close_all(root_fd, name, state):
    unresolved = []
    for row in state["sessions"][:]:
        profile = "session-" + row["session_id"]
        expected = row["profile_inode"]
        try:
            check_profile(root_fd, profile, expected)
            if "custody" in row:
                m7.private_ram_root(os.readlink(f"/proc/self/fd/{root_fd}"))
                signals, closure = close_m7(row)
                m7_witness = {"m7": closure}
            else:
                signals, m7_witness = stop(row), {}
            present = check_profile(root_fd, profile, expected)
            if present:
                require(shutil.rmtree.avoids_symlink_attacks, "profile_identity",
                        "descriptor-based tree removal required")
                shutil.rmtree(profile, dir_fd=root_fd)
            require(not check_profile(root_fd, profile, expected),
                    "profile_residual", "recorded profile remains after teardown")
        except (ValueError, OSError) as error:
            unresolved.append((row["session_id"], error))
            continue
        state["close_witnesses"].append({
            "session_id": row["session_id"], "pid": row["pid"], "starttime": row["starttime"],
            "pgid": row["pgid"], "process_absent": True, "profile_absent": True,
            "profile_removed": present, "signals": signals, **m7_witness,
            "profile": {"name": profile, "inode": expected,
                        "disposition": "removed" if present else "already_absent",
                        "absence_witness": "directory_entry_enoent"},
        })
        state["sessions"].remove(row)
        write_state(root_fd, state, name)
    if len(unresolved) == 1:
        raise unresolved[0][1]
    if unresolved:
        raise Refused("close_partial", "recorded sessions still require reconciliation",
                      unresolved=[{"session_id": ident, "detail": str(error)} for ident, error in unresolved])
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


def verify_transport_display(path, display_number):
    transport = m7.setup.decode(m7.setup.read(path, private=True))
    raw = transport.get("display") if type(transport) is dict else None
    display = re.fullmatch(r":([0-9]+)(?:\.[0-9]+)?", raw) if type(raw) is str else None
    require(display and int(display[1]) >= 90 and int(display[1]) == display_number,
            "transport_display", "UI transport must use the requested private display :90 or higher")
    m7.ui_transport(transport)


def preflight_m7(args):
    registry = m7.setup.registry_path()
    try:
        raw = m7.setup.read(registry, private=True)
    except FileNotFoundError:
        raise Refused("m7_registry_missing", "operator ARM64 registry required", registry=str(registry)) from None
    require(args.launcher_grant is not None, "launcher_grant", "explicit operator launcher grant required")
    require(args.ui_transport is not None, "transport_display", "explicit UI transport required")
    directory = m7.setup.private_dir(registry.parent)
    registry_sha = m7.setup.digest(raw)
    m7.bind_multi_grant(args.launcher_grant, registry_sha)
    m7.private_ram_root(args.profile_root)
    value = m7.setup.validate(m7.setup.decode(raw))
    packaged_helper = value["producer_pins"]["setup"]
    packaged_setup = Path(packaged_helper["path"]).with_name("arm64_capsules.py")
    require(packaged_helper["sha256"] == m7.setup.digest(m7.setup.read(m7.__file__, 1 << 20))
            and m7.setup.read(packaged_setup, 1 << 20) == m7.setup.read(m7.setup.__file__, 1 << 20),
            "m7_package_mismatch", "registered package must contain this M7 implementation")
    try:
        peak = m7.preflight_multi(directory, args.n)
    except m7.MultiMemoryRefused as error:
        raise Refused("memory_floor", str(error)) from error
    return value, registry_sha, directory, peak


def wait_m7_ready(child, row, directory):
    slot = m7.multi_slot(directory, row["session_id"])
    deadline = time.monotonic() + READY_SECONDS
    while time.monotonic() < deadline:
        require(child.poll() is None, "readiness", "M7 exited before app binding")
        if not m7.present(slot / "held-session.json"):
            time.sleep(0.05)
            continue
        root, outer, unit = m7_plan(slot, row)
        require(m7.process_alive(outer), "process_identity", "M7 outer identity changed")
        try:
            raw = m7.setup.read(root / "app-ready.json", private=True)
        except FileNotFoundError:
            time.sleep(0.05)
            continue
        app = m7.setup.decode(raw)
        require(m7.process_alive(app) and app["cgroup"].endswith("/" + unit),
                "readiness", "owned M7 app binding changed")
        rss = resident_kib(app["pid"])
        require(m7.process_alive(app), "readiness", "owned M7 app exited during observation")
        return {"ready_witness": "m7_app_bound", "app_pid": app["pid"], "app_rss_kib": rss}
    raise Refused("readiness", "M7 app binding deadline expired")


def launch_m7(args, backend, root_fd, checkpoint):
    value, registry_sha, directory, _ = backend
    m7.setup.executable(value["python"])
    m7.setup.pin(value["producer_pins"]["setup"], 1 << 20)
    ident = secrets.token_hex(12)
    profile = "session-" + ident
    os.mkdir(profile, mode=0o700, dir_fd=root_fd)
    profile_fd = os.open(profile, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
    try:
        with os.fdopen(os.open("launcher.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                              0o600, dir_fd=profile_fd), "wb") as log:
            child = subprocess.Popen(
                [value["python"]["path"], "-I", "-B", value["producer_pins"]["setup"]["path"],
                 "--app", value["package"]["app"]["path"], "--source-root", value["source_root"],
                 "--expected-commit", value["source_commit"], "--ui-transport", args.ui_transport,
                 "--multi-grant", args.launcher_grant, "--multi-run-id", ident, "--multi-display", args.display],
                start_new_session=True, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT)
        with child.stdin:
            identity = m7.process_identity(child.pid)
            require(identity is not None, "process_identity", "M7 outer identity unknown")
            row = {"session_id": ident, "pid": child.pid, "starttime": identity["start_ticks"],
                   "pgid": child.pid, "profile_inode": inode(os.fstat(profile_fd)),
                   "custody": {"registry": str(directory), "registry_inode": inode(directory.stat()),
                               "registry_sha256": registry_sha, "outer_cgroup": identity["cgroup"]}}
            checkpoint(row)
            child.stdin.write(b"1")
        return row, lambda: wait_m7_ready(child, row, directory)
    finally:
        os.close(profile_fd)


def resident_kib(pid):
    rows = [line for line in Path(f"/proc/{pid}/status").read_text().splitlines()
            if line.startswith("VmRSS:")]
    observed = re.fullmatch(r"VmRSS:\s+([0-9]+) kB", rows[0]) if len(rows) == 1 else None
    require(observed is not None, "rss_unknown", "process VmRSS must be present once in kB")
    return int(observed[1])


def open_ramp(args, launch=None):
    """Reserve and record each owned launch before waiting for its readiness signal."""
    require(args.n > 0, "count", "positive capsule count required")
    display = re.fullmatch(r":([0-9]+)", args.display)
    ambient = os.environ.get("DISPLAY")
    require(display and int(display[1]) >= 90 and (ambient is None or ambient == args.display),
            "display", "ambient DISPLAY must be unset or equal the requested private display :90 or higher")
    verify_scope(args.scope_unit)
    if args.ui_transport is not None:
        verify_transport_display(args.ui_transport, int(display[1]))
    with locked_root(args.profile_root) as root_fd:
        state = read_state(root_fd, allow_missing=True)
        recorded = state["sessions"]
        # Refuse an unaffordable batch before the first launch as well as
        # rechecking current memory immediately before launching each slot.
        owned = sum("custody" not in row for row in recorded)
        admit(available_kib(), (owned + args.n - 1) * SESSION_RESERVATION_KIB)
        backend = preflight_m7(args) if launch is None else None

        def checkpoint(row):
            ident, pid = validate_session(row)
            require(all(other["pid"] != pid and other["session_id"] != ident for other in recorded),
                    "state", "duplicate session identity")
            require(process_identity(pid) == (row["starttime"], row["pgid"]),
                    "process_identity", "launch identity changed")
            require(check_profile(root_fd, "session-" + ident, row["profile_inode"]),
                    "profile_identity", "disposable profile is absent")
            recorded.append(row)
            write_state(root_fd, state)

        sessions = []
        for _ in range(args.n):
            started = time.monotonic_ns()
            if launch is None:
                peak = backend[-1]
                try:
                    m7.preflight_multi(backend[2], 1)
                except m7.MultiMemoryRefused as error:
                    raise Refused("memory_floor", str(error)) from error
                row, wait_ready = launch_m7(args, backend, root_fd, checkpoint)
            else:
                peak = admit(available_kib(), len(recorded) * SESSION_RESERVATION_KIB)
                row, wait_ready = launch(args)
                checkpoint(row)
            ident, pid = row["session_id"], row["pid"]
            identity = row["starttime"], row["pgid"]
            witness = wait_ready()
            ready_ms = (time.monotonic_ns() - started) // 1_000_000
            readiness = witness if backend else {"ready_witness": witness}
            require(type(readiness.get("ready_witness")) is str and bool(readiness["ready_witness"].strip()),
                    "readiness", "capsule readiness signal is absent")
            rss = resident_kib(pid)
            require(process_identity(pid) == identity, "process_identity", "ready identity changed")
            sessions.append({"session_id": ident, "pid": pid, "starttime": identity[0],
                             "pgid": identity[1], "rss_kib": rss, "ready_ms": ready_ms, **readiness})
        return {"sessions": sessions, "peak_reservation_kib": peak, "stop_owner": state["stop_owner"]}


def main(argv=None, *, launch=None):
    parser = argparse.ArgumentParser(description=__doc__)
    verbs = parser.add_subparsers(dest="verb", required=True)
    opening = verbs.add_parser("open")
    opening.add_argument("--n", type=int, required=True)
    opening.add_argument("--scope-unit", required=True)
    opening.add_argument("--profile-root", required=True)
    opening.add_argument("--display", required=True)
    opening.add_argument("--ui-transport", metavar="F")
    opening.add_argument("--launcher-grant", metavar="F")
    closing = verbs.add_parser("close")
    closing.add_argument("--all-recorded", action="store_true", required=True)
    closing.add_argument("--state", required=True)
    verbs.add_parser("status").add_argument("--state", required=True)
    args = parser.parse_args(argv)
    try:
        if args.verb == "open":
            result = open_ramp(args, launch)
        else:
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
