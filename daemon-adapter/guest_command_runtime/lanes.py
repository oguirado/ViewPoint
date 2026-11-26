"""Trusted ingress for the two Atalaya command lanes.

The guest ingress process uses local Unix peer credentials. Its caller must
already have validated the host lease and created the per-lane ingress peers
and job cgroups. Public v0.2 command requests continue through Controller.
"""

from dataclasses import dataclass
import os
from pathlib import Path
import select
import socket
import stat
import struct
from threading import Lock


class LaneDenied(PermissionError):
    pass


@dataclass(frozen=True)
class Lane:
    number: int
    cgroup: Path
    device: int
    inode: int

    @property
    def uid(self):
        return 10000 + self.number


class LaneIngress:
    def __init__(self, lanes_root):
        lanes_root = Path(lanes_root).resolve(strict=True)
        self._hierarchy = Path('/sys/fs/cgroup')
        if not lanes_root.is_relative_to(self._hierarchy):
            raise LaneDenied('lanes must belong to the admitted hierarchy')
        self._lanes = tuple(self._lane(lanes_root, number) for number in (0, 1))
        if len({(lane.device, lane.inode) for lane in self._lanes}) != len(self._lanes):
            raise LaneDenied('lane cgroups must be distinct')
        self._locks = tuple(Lock() for _ in self._lanes)

    @staticmethod
    def _lane(root, number):
        path = root / str(number)
        status = path.stat(follow_symlinks=False)
        if not stat.S_ISDIR(status.st_mode) or status.st_uid != 0 or status.st_mode & 0o022:
            raise LaneDenied('cgroup must be a broker-owned directory')
        return Lane(number, path, status.st_dev, status.st_ino)

    def dispatch(self, host_lane, peer, request, run):
        if type(host_lane) is not int or host_lane not in (0, 1):
            raise LaneDenied('host lane binding required')
        if type(request) is not dict:
            raise LaneDenied('request must be an object')
        pending = [request]
        visited = set()
        while pending:
            value = pending.pop()
            if not isinstance(value, (dict, list)) or id(value) in visited:
                continue
            visited.add(id(value))
            if isinstance(value, dict):
                if 'lane_id' in value:
                    raise LaneDenied('request contains a caller-selected lane')
                pending.extend(value.values())
            else:
                pending.extend(value)
        if peer.family != socket.AF_UNIX or peer.type != socket.SOCK_STREAM:
            raise LaneDenied('Unix stream credentials required')
        lane = self._lanes[host_lane]
        with self._locks[host_lane]:
            try:
                pid, uid, gid = struct.unpack('3i', peer.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
                if pid <= 0 or uid != lane.uid or gid != lane.uid:
                    raise LaneDenied('peer credentials do not match host lane')
                # The trusted relay keeps the connecting process alive until it
                # receives a reply. Pin its PID before reading procfs and reject
                # exit on either side of the membership observation.
                pidfd = os.pidfd_open(pid)
                try:
                    poll = select.poll()
                    poll.register(pidfd, select.POLLIN)
                    if poll.poll(0):
                        raise LaneDenied('peer exited before identity check')
                    relative = '/' + lane.cgroup.relative_to(self._hierarchy).as_posix()
                    with open(f'/proc/{pid}/cgroup') as member:
                        membership = member.read().splitlines()
                    status = lane.cgroup.stat(follow_symlinks=False)
                    if (poll.poll(0) or membership != ['0::' + relative]
                            or (status.st_dev, status.st_ino) != (lane.device, lane.inode)):
                        raise LaneDenied('peer cgroup does not match host lane')
                finally:
                    os.close(pidfd)
            except OSError as error:
                if isinstance(error, LaneDenied):
                    raise
                raise LaneDenied('cannot verify live lane peer') from error
            return run(lane, request)
