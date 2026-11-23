"""Expose the host's read-only attach inventory to the isolated T2 worker."""

from contextlib import contextmanager
import json
from pathlib import Path
import re
from socketserver import StreamRequestHandler, UnixStreamServer
import subprocess
from threading import Thread


def process_inventory(proc=Path("/proc")):
    guests, displays = [], []
    for path in proc.iterdir():
        if not path.name.isdecimal():
            continue
        try:
            raw = (path / "stat").read_text(errors="replace")
        except (FileNotFoundError, ProcessLookupError):
            continue
        head, fields = raw.rsplit(")", 1)
        name = head.split("(", 1)[1]
        tail = fields.split()
        if tail[0] in ("Z", "X"):
            continue
        identity = f"{name}:{path.name}:{tail[19]}"
        if name in {"Xvfb", "Xephyr", "Xnest", "Xwayland", "xpra"}:
            displays.append(identity)
        if name.startswith("qemu-system") or name in {"firecracker", "cloud-hyperviso", "crosvm", "kvmtool", "lkvm"}:
            guests.append(identity)
    return {"guests": sorted(guests), "private_display_servers": sorted(displays)}


def host_inventory(config):
    raw = subprocess.check_output(
        ["/usr/bin/docker", "--config", str(config), "--host", "unix:///var/run/docker.sock",
         "container", "ls", "--all", "--no-trunc", "--format", "{{.ID}}"],
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, text=True, timeout=5,
    )
    containers = raw.splitlines()
    if len(containers) != len(set(containers)) or any(not re.fullmatch(r"[0-9a-f]{64}", row) for row in containers):
        raise ValueError("invalid host container inventory")
    return dict(process_inventory(), containers=sorted(containers))


@contextmanager
def serve(work):
    config = work / "docker-empty"
    config.mkdir(mode=0o700)

    class InventoryRequest(StreamRequestHandler):
        def handle(self):
            try:
                snapshot = host_inventory(config)
            except (OSError, ValueError, subprocess.SubprocessError):
                snapshot = {"error": "host attach inventory AUSENTE"}
            try:
                self.wfile.write(json.dumps(snapshot).encode())
            except BrokenPipeError:
                pass

    endpoint = work / "inventory.sock"
    with UnixStreamServer(str(endpoint), InventoryRequest) as server:
        endpoint.chmod(0o600)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield
        finally:
            server.shutdown()
            thread.join()
