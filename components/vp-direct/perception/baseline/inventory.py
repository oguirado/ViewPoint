"""Account disjoint cgroup v2 scopes and record process PSS as evidence."""

import os
from pathlib import Path, PurePosixPath
import sys

# Use the parsers from the same checkout under the isolated interpreter.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "wender-agent"))
from wender.learn.cgroup_memory import CgroupMemoryError, forbid_invalid_ram_arithmetic, parse_cgroup_memory
from wender.learn.procfs_memory import ProcfsMemoryError, parse_proc_stat, parse_smaps_rollup


class IncompleteInventory(ValueError):
    """A scope could not be inventoried without ambiguity."""


_GPU_STATUS = "unsupported"


def _unified_cgroup(text):
    paths = [line[3:] for line in text.splitlines() if line.startswith("0::")]
    if len(paths) != 1:
        raise IncompleteInventory("expected one cgroup v2 membership")
    return paths[0]


def _check_scope(path, uid):
    scope = PurePosixPath(path)
    user_slice = PurePosixPath(f"/user.slice/user-{uid}.slice")
    if (
        str(scope) != path
        or ".." in scope.parts
        or not scope.is_relative_to(user_slice)
        or any(ord(character) < 32 for character in path)
    ):
        raise IncompleteInventory("scope must be a canonical path in the current user's slice")


def _read(path):
    return path.read_text(encoding="utf-8")


def _raise_walk_error(error):
    raise error


def _members(directory, cgroup):
    members = {}
    for root, dirs, _ in os.walk(directory, onerror=_raise_walk_error):
        root = Path(root)
        if any((root / name).is_symlink() for name in dirs):
            raise IncompleteInventory("unexpected symlink in cgroup tree")
        path = str(PurePosixPath(cgroup) / root.relative_to(directory))
        for line in _read(root / "cgroup.procs").splitlines():
            if not line.isascii() or not line.isdecimal() or int(line) <= 0:
                raise IncompleteInventory("invalid cgroup process identifier")
            pid = int(line)
            if pid in members:
                # cgroup.procs may repeat a PID. A different directory means
                # membership moved during the walk.
                if members[pid] != path:
                    raise IncompleteInventory(f"process {pid} moved during the cgroup census")
            members[pid] = path
    return members


def _read_process_file(directory_fd, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    with os.fdopen(fd, encoding="utf-8") as stream:
        return stream.read()


def _check_uid(directory_fd, uid, pid):
    if os.fstat(directory_fd).st_uid != uid:
        raise IncompleteInventory(f"ownership of process {pid} could not be verified")
    status = _read_process_file(directory_fd, "status")
    uids = [line.split()[1:] for line in status.splitlines() if line.startswith("Uid:")]
    if uids != [[str(uid)] * 4]:
        raise IncompleteInventory(f"ownership of process {pid} could not be verified")


def _process(proc_root, pid, cgroup, uid):
    # A directory descriptor keeps reads bound to the same procfs incarnation.
    directory_fd = os.open(proc_root / str(pid), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _check_uid(directory_fd, uid, pid)
        before = parse_proc_stat(_read_process_file(directory_fd, "stat"), expected_pid=pid)
        if _unified_cgroup(_read_process_file(directory_fd, "cgroup")) != cgroup:
            raise IncompleteInventory(f"process {pid} changed cgroups")
        pss = parse_smaps_rollup(_read_process_file(directory_fd, "smaps_rollup")).pss_bytes
        after = parse_proc_stat(_read_process_file(directory_fd, "stat"), expected_pid=pid)
        _check_uid(directory_fd, uid, pid)
        if before.starttime != after.starttime:
            raise IncompleteInventory(f"process {pid} changed incarnation")
        if _unified_cgroup(_read_process_file(directory_fd, "cgroup")) != cgroup:
            raise IncompleteInventory(f"process {pid} changed cgroups")
    finally:
        os.close(directory_fd)
    return {"pid": pid, "starttime_ticks": before.starttime, "pss_bytes": pss}


def _sample_scope(cgroup_root, proc_root, cgroup, owner, uid):
    directory = cgroup_root / cgroup[1:]
    if directory.resolve(strict=True) != directory:
        raise IncompleteInventory("scope must resolve without aliases")
    identity = directory.stat()
    members = _members(directory, cgroup)
    processes = [_process(proc_root, pid, members[pid], uid) for pid in sorted(members)]
    # This gauge already includes descendant cgroups.
    current = parse_cgroup_memory(_read(directory / "memory.current")).current_bytes
    final_identity = directory.stat()
    if (
        members != _members(directory, cgroup)
        or (identity.st_dev, identity.st_ino) != (final_identity.st_dev, final_identity.st_ino)
    ):
        raise IncompleteInventory("cgroup changed during sampling")
    return {
        "owner": owner,
        "cgroup": cgroup,
        "current_bytes": current,
        "processes": processes,
    }


def _charged_bytes(scopes):
    return sum(scope["current_bytes"] for scope in scopes)


def collect(scopes=None, *, proc_root=Path("/proc"), cgroup_root=Path("/sys/fs/cgroup")):
    """Return a complete inventory or raise, using the caller's cgroup by default.

    Explicit scopes are (owner, absolute cgroup v2 path) pairs. The alternate
    roots are for fixture injection. The CLI always uses the kernel roots.
    """
    uid = os.getuid()
    if uid != os.geteuid():
        raise IncompleteInventory("real and effective user IDs must agree")
    try:
        if scopes is None:
            scopes = [("caller", _unified_cgroup(_read(proc_root / "self" / "cgroup")))]
        cgroups = {}
        for owner, path in scopes:
            if not owner.strip():
                raise IncompleteInventory("each scope requires an owner")
            _check_scope(path, uid)
            for other in cgroups:
                try:
                    forbid_invalid_ram_arithmetic(ancestor_cgroup_path=other, child_cgroup_path=path)
                except CgroupMemoryError as error:
                    raise IncompleteInventory("admitted cgroup scopes overlap") from error
            cgroups[path] = owner
        if not cgroups:
            raise IncompleteInventory("no scopes were admitted")

        rows = []
        seen_pids = set()
        for path in sorted(cgroups):
            row = _sample_scope(cgroup_root, proc_root, path, cgroups[path], uid)
            pids = {process["pid"] for process in row["processes"]}
            if seen_pids & pids:
                raise IncompleteInventory("process appeared in multiple scopes")
            seen_pids.update(pids)
            rows.append(row)
    except (OSError, UnicodeError, ProcfsMemoryError, CgroupMemoryError) as error:
        raise IncompleteInventory("required procfs or cgroup evidence unavailable or invalid") from error

    return {
        "complete": True,
        "scopes": rows,
        "charged_bytes": _charged_bytes(rows),
        "gpu": {"status": _GPU_STATUS},
    }
