# SPDX-License-Identifier: AGPL-3.0-only
"""Linux session owner for a registered, synchronous adapter command."""

import ctypes
import json
import os
from pathlib import Path
from secrets import token_hex
import subprocess
import sys
import tempfile
import time


# The launcher uses -I. Resolve the factory package from this script's tree.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from asset_factory.proc_pidns import terminate_owned_child


MAX_REPLY_BYTES = 128 * 1024


def stop_children(children_file):
    """Reap adopted descendants, signalling only unreaped direct children."""
    killed = False
    while children := children_file.read_text().split():
        killed = True
        for child in map(int, children):
            try:
                terminate_owned_child(child)
            except ProcessLookupError:
                pass
            os.waitpid(child, 0)
    return killed


def supervise(request):
    # Only this short-lived owner adopts grandchildren, including detached ones.
    pr_set_child_subreaper = 36
    if ctypes.CDLL(None, use_errno=True).prctl(pr_set_child_subreaper, 1, 0, 0, 0):
        raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER")
    children_file = Path(f"/proc/self/task/{os.getpid()}/children")
    started_ns = time.monotonic_ns()
    if started_ns >= request["expires_ns"]:
        return {"error": "EXPIRED_GRANT"}

    with tempfile.TemporaryFile(dir=".") as stdout:
        try:
            child = subprocess.Popen(
                request["argv"], stdin=subprocess.PIPE, stdout=stdout,
                stderr=None, start_new_session=True,
            )
        except OSError:
            return {"error": "ADAPTER_START_FAILED"}
        process = {"pid": child.pid, "nonce": token_hex(16)}
        message = {**request["message"], "process": process}
        try:
            remaining = (request["expires_ns"] - time.monotonic_ns()) / 1_000_000_000
            if remaining <= 0:
                return {"error": "EXPIRED_GRANT"}
            child.communicate(json.dumps(message).encode(), timeout=remaining)
        except subprocess.TimeoutExpired:
            return {"error": "EXPIRED_GRANT"}
        finally:
            if child.poll() is None:
                terminate_owned_child(child.pid)
                child.wait()
            if child.stdin is not None:
                child.stdin.close()
            descendants = stop_children(children_file)

        finished_ns = time.monotonic_ns()
        if finished_ns >= request["expires_ns"]:
            return {"error": "EXPIRED_GRANT"}
        if descendants:
            return {"error": "ADAPTER_DESCENDANTS"}
        if child.returncode:
            return {"error": "ADAPTER_FAILED"}
        stdout.seek(0)
        output = stdout.read(MAX_REPLY_BYTES + 1)
        try:
            if len(output) > MAX_REPLY_BYTES:
                raise ValueError("oversized")
            reply = json.loads(output)
        except ValueError:
            return {"error": "ADAPTER_REPLY_INVALID"}
        return {"process": process, "start_ns": started_ns, "finish_ns": finished_ns,
                "reply": reply}


if __name__ == "__main__":
    print(json.dumps(supervise(json.load(sys.stdin))))
