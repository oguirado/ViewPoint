"""Executed only inside the fresh network namespace created by the runner."""

import errno
import fcntl
import json
import os
from pathlib import Path
import socket
import struct
import sys


PROBE_MESSAGE = "QUAL_FETCH_DENIED"


def establish_namespace(caller_inode):
    inode = os.stat("/proc/self/ns/net").st_ino
    if inode == caller_inode:
        raise RuntimeError("offline_namespace_unproven")
    devices = Path("/proc/self/net/dev").read_text().splitlines()[2:]
    interfaces = sorted(row.partition(":")[0].strip() for row in devices)
    ipv4 = Path("/proc/self/net/route").read_text().splitlines()
    ipv6 = Path("/proc/self/net/ipv6_route").read_text().splitlines()
    # The fresh namespace may list the IPv6 reject routes via loopback.
    only_reject_routes = all(
        row.split()[-1] == "lo" and int(row.split()[-2], 16) & 0x200 for row in ipv6
    )
    with socket.socket() as sock:
        reply = fcntl.ioctl(sock.fileno(), 0x8913, struct.pack("256s", b"lo"))  # SIOCGIFFLAGS
    loopback_up = bool(struct.unpack_from("H", reply, 16)[0] & 1)
    ipv4_empty = not ipv4 or (len(ipv4) == 1 and ipv4[0].startswith("Iface"))
    if interfaces != ["lo"] or loopback_up or not ipv4_empty or not only_reject_routes:
        raise RuntimeError("offline_namespace_unproven")
    return {"inode": inode, "interfaces": interfaces, "loopback_up": False, "routes": False}


def fetch_probe():
    with socket.socket() as sock:
        try:
            # RFC 5737 TEST-NET-1. The inherited kernel filter denies connect.
            sock.connect(("192.0.2.1", 443))
        except OSError as exc:
            if exc.errno == errno.EACCES:
                print(PROBE_MESSAGE)
                return 0
    return 5


def main(argv):
    if argv == ["probe"]:
        return fetch_probe()
    caller_inode, destination, command, *args = argv
    evidence = establish_namespace(int(caller_inode))
    with Path(destination).open("x") as stream:
        json.dump(evidence, stream, sort_keys=True)
    os.execv(command, [command, *args])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
