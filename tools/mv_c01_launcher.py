#!/usr/bin/env python3
"""Manage the private RAM directory for an approved Medicion C01 coordinator."""

import argparse
from contextlib import ExitStack
import os
from pathlib import Path
import re
import subprocess
import sys

if __package__:
    from . import mv_capsule_ramp as ramp
else:
    import mv_capsule_ramp as ramp

m7 = ramp.m7
PROFILES = {"cold", "same-owner-reuse", "sustained-mixed", "arrival-burst"}
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def need(condition, reason):
    m7.setup.need(condition, reason)


def assignments(approval):
    runtime = Path(approval["runtime_root"])
    need(runtime.parent == m7.shm_base() and runtime.name not in (".", ".."),
         "C01 runtime must be a new direct child of /dev/shm")
    profiles = approval["profiles"]
    need(type(profiles) is dict and profiles and profiles.keys() <= PROFILES,
         "invalid C01 profiles")
    blocks = []
    for profile, spec in profiles.items():
        count = spec["blocks"]
        # Medicion formats block numbers as two-digit directory names.
        need(type(count) is int and 1 <= count <= 99, "invalid C01 block count")
        blocks.extend(f"{profile}-{number:02}" for number in range(1, count + 1))
    return runtime, blocks


def remove_runtime(base_fd, runtime_fd, runtime, blocks, output):
    """Archive settled ledgers before deleting any of the known entries."""
    identity = ramp.inode(os.fstat(runtime_fd))
    need(ramp.inode(os.stat(runtime.name, dir_fd=base_fd, follow_symlinks=False)) == identity
         and sorted(os.listdir(runtime_fd)) == sorted(blocks), "C01 runtime identity or entries changed")
    with ExitStack() as locks:
        ledgers = {}
        block_fds = {}
        for name, expected in blocks.items():
            fd = locks.enter_context(ramp.locked_root(runtime / name))
            need(ramp.inode(os.fstat(fd)) == expected, "C01 runtime block replaced")
            block_fds[name] = fd
            entries = os.listdir(fd)
            need(entries in ([], [ramp.STATE_NAME]), "C01 runtime block contains residual files")
            if entries:
                state = ramp.read_state(fd)
                witnesses = state["close_witnesses"]
                need(not state["sessions"] and witnesses and all(
                    type(row) is dict and row.get("process_absent") is True
                    and row.get("profile_absent") is True and type(row.get("m7")) is dict
                    for row in witnesses), "C01 runtime ledger is unsettled")
                ledgers[name] = state
        for name, state in ledgers.items():
            block_output = m7.setup.private_dir(output / name)
            m7.setup.write_new(block_output / ramp.STATE_NAME, state)
        for name, fd in block_fds.items():
            need(ramp.inode(os.stat(name, dir_fd=runtime_fd, follow_symlinks=False)) == blocks[name],
                 "C01 runtime block identity changed before removal")
            if name in ledgers:
                os.unlink(ramp.STATE_NAME, dir_fd=fd)
            os.rmdir(name, dir_fd=runtime_fd)
            need(os.fstat(fd).st_nlink == 0, "C01 runtime block removal unproved")
        need(ramp.inode(os.stat(runtime.name, dir_fd=base_fd, follow_symlinks=False)) == identity,
             "C01 runtime root identity changed before removal")
        os.rmdir(runtime.name, dir_fd=base_fd)
        need(os.fstat(runtime_fd).st_nlink == 0 and not m7.present(runtime),
             "C01 runtime root removal unproved")
    return list(ledgers)


def launch(coordinator, approval_path, output):
    surface = coordinator.parents[1]
    need(coordinator.resolve(strict=True) == coordinator and coordinator.is_file(),
         "canonical C01 coordinator required")
    need(approval_path.resolve(strict=True) == approval_path and approval_path.is_relative_to(surface),
         "approval outside the C01 surface")
    need(output.is_absolute() and output.resolve() == output and output != surface
         and output.is_relative_to(surface) and not m7.present(output), "fresh C01 output required")
    witness = output.with_name(output.name + ".runtime-removal.json")
    need(not m7.present(witness), "C01 runtime removal witness already exists")
    approval = m7.setup.decode(m7.setup.read(approval_path, private=True))
    need(approval["schema"] == "viewpoint-c01-live-approval/v1" and approval["decision"] == "ADMIT",
         "C01 approval required")
    need(type(approval["scope_unit"]) is str and re.fullmatch(r"[A-Za-z0-9_.@-]+\.scope", approval["scope_unit"]),
         "explicit approved scope required")
    ramp.verify_scope(approval["scope_unit"])
    group = Path("/proc/self/cgroup").read_text().strip().removeprefix("0::")
    m7.verify_guardian_cpus(group)
    need((Path("/sys/fs/cgroup" + group) / "memory.swap.max").read_text().strip() == "0",
         "C01 swap cap required")
    display = approval["display"]
    need(type(display) is str and re.fullmatch(r":[0-9]+", display) and int(display[1:]) >= 90
         and os.environ.get("DISPLAY") in (None, display), "approved private display required")
    runtime, names = assignments(approval)

    with ExitStack() as descriptors:
        base_fd = os.open(runtime.parent, DIRECTORY)
        descriptors.callback(os.close, base_fd)
        os.mkdir(runtime.name, mode=0o700, dir_fd=base_fd)
        runtime_fd = os.open(runtime.name, DIRECTORY, dir_fd=base_fd)
        descriptors.callback(os.close, runtime_fd)
        receipt = {"schema": "viewpoint-c01-runtime-removal/v1", "runtime_root": str(runtime),
                   "root_inode": ramp.inode(os.fstat(runtime_fd)), "disposition": "preserved"}
        blocks = {}
        started = False
        try:
            m7.private_ram_root(runtime)
            for name in names:
                os.mkdir(name, mode=0o700, dir_fd=runtime_fd)
                blocks[name] = ramp.inode(os.stat(name, dir_fd=runtime_fd, follow_symlinks=False))
            # live.py remains the direct parent of every ramp command. The
            # wrapper neither consumes nor issues the coordinator's grant.
            child = subprocess.Popen([sys.executable, "-B", str(coordinator), "--approval",
                                      str(approval_path), "--out", str(output)])
            started = True
            receipt["coordinator_returncode"] = child.wait()
            if receipt["coordinator_returncode"] != 0:
                return 2
            m7.setup.private_dir(output)
            receipt["archived_ledgers"] = remove_runtime(base_fd, runtime_fd, runtime, blocks, output)
            receipt["disposition"] = "removed"
            receipt["absence_witness"] = "owned_directory_unlinked_and_path_enoent"
            return 0
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            receipt["error"] = str(error)
            if not started:
                receipt["archived_ledgers"] = remove_runtime(base_fd, runtime_fd, runtime, blocks, output)
                receipt["disposition"] = "removed"
                receipt["absence_witness"] = "owned_directory_unlinked_and_path_enoent"
            raise
        finally:
            m7.setup.write_new(witness, receipt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coordinator", type=Path, required=True)
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        return launch(args.coordinator, args.approval, args.out)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
