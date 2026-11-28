# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only probe for a host-registered local accessibility endpoint.

Register this file in Adapter.probe_argv after the Python executable. Supply
CAPSULE INCARNATION VIEW followed by one of these endpoint specifications:

    cdp SOCKET PEER_UID WINDOW_ID
    at_spi SOCKET BUS_NAME PROVIDER_PID OBJECT_PATH CONTROL_PATH

Use a private endpoint for the selected capsule. Pin the probe and its
transport dependencies in the adapter. The request cannot select an endpoint.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from secrets import randbelow
import socket
import stat
from struct import unpack
import sys


def _private_socket(path):
    endpoint = Path(path)
    parent = endpoint.parent.stat()
    if (not endpoint.is_absolute() or not stat.S_ISSOCK(endpoint.lstat().st_mode)
            or parent.st_uid != os.getuid() or parent.st_mode & 0o077):
        raise ValueError("Expected a private Unix socket")
    return endpoint


def _cdp(path, uid, window, node):
    seq = randbelow(999999) + 1
    request = {"op": "ax", "frame_seq": seq, "window_title_or_index": window}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(4)
        connection.connect(str(_private_socket(path)))
        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        _, peer, _ = unpack("3i", credentials)
        if peer != int(uid):
            raise ValueError("Wrong CDP peer")
        connection.sendall(json.dumps(request).encode() + b"\n")
        limit = 16 * 1024 * 1024
        with connection.makefile("rb") as reader:
            raw = reader.readline(limit + 1)
    if len(raw) > limit or not raw.endswith(b"\n"):
        raise ValueError("Invalid CDP snapshot framing")
    snapshot = json.loads(raw)
    if (snapshot["frame_seq"] != seq or snapshot["window"]["id"] != window
            or snapshot["truncated"] is not False):
        raise ValueError("Wrong or incomplete CDP snapshot")

    # OpenSuperficies' AX shim publishes the window-scoped hash of the CDP ID.
    backend = node["backend_node_id"]
    node_id = sha256(f"{window}:{backend}".encode()).hexdigest()[:24]
    match, = (item for item in snapshot["elements"] if item["id"] == node_id)
    return {"backend_node_id": backend, "name": match["name"]}


def _at_spi(path, bus, pid, object_path, control_path):
    from wender.perceive.atspi import _call
    from wender.perceive.atspi_tree import _string

    endpoint = _private_socket(path)
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(endpoint)) or not re.fullmatch(r":\d+\.\d+", bus):
        raise ValueError("Invalid AT-SPI endpoint")
    address = "unix:path=" + str(endpoint)
    owner = _call(address, "org.freedesktop.DBus", "/org/freedesktop/DBus",
                  "org.freedesktop.DBus.GetConnectionUnixProcessID", bus, timeout=2)
    if owner != f"(uint32 {int(pid)},)":
        raise ValueError("Wrong AT-SPI peer")
    get = "org.freedesktop.DBus.Properties.Get"
    interface = "org.a11y.atspi.Accessible"
    name = _string(_call(address, bus, object_path, get, interface, "Name", timeout=2))
    state = _string(_call(address, bus, object_path, get, interface, "Description", timeout=2))
    if not name or not state:
        raise ValueError("Missing AT-SPI properties")
    return {"control_path": control_path, "name": name, "ui_state": state}


def main(argv):
    message = json.load(sys.stdin)
    reply = {key: message[key] for key in ("call", "lease_ref", "process")}
    reply["status"] = "unavailable"
    try:
        capsule, incarnation, view, channel, *endpoint = argv
        target = {"capsule_id": capsule, "incarnation": incarnation, "view_id": view}
        if (any(message["call"][key] != target[key] for key in ("capsule_id", "incarnation"))
                or message["call"]["channel"] != channel or message["arguments"]["target"] != target):
            raise ValueError("Wrong admitted target")
        if channel == "cdp" and len(endpoint) == 3:
            value = _cdp(*endpoint, message["arguments"]["node"])
        elif channel == "at_spi" and len(endpoint) == 5:
            value = _at_spi(*endpoint)
        else:
            raise ValueError("Unknown probe")
        reply.update(status="observed", observation={"target": target, "value": value})
    except (ImportError, KeyError, OSError, TypeError, ValueError):
        pass
    print(json.dumps(reply))


if __name__ == "__main__":
    main(sys.argv[1:])
