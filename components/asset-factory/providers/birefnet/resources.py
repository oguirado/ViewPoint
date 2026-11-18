"""Read-only Linux cgroup-v2 observations for CPU image work.

Ancestor limits, including ancestor usage by siblings, constrain headroom.
An explicitly unlimited limit is distinct from a missing observation.
"""
import os
from pathlib import Path


class ObservationUnavailable(Exception):
    pass


def _unescape(value):
    for encoded, decoded in ((r'\040', ' '), (r'\011', '\t'), (r'\012', '\n'), (r'\134', '\\')):
        value = value.replace(encoded, decoded)
    return value


def _limit(path):
    raw = path.read_text().strip()
    if raw == 'max':
        return None
    if not raw.isascii() or not raw.isdecimal():
        raise ValueError('invalid cgroup limit')
    return int(raw)


def cgroup_observation(*, membership=None, mountinfo=None):
    """Observe the visible hierarchy; hidden ancestors cannot prove a bound."""
    try:
        membership = Path('/proc/self/cgroup') if membership is None else Path(membership)
        mountinfo = Path('/proc/self/mountinfo') if mountinfo is None else Path(mountinfo)
        entries = [line[3:] for line in membership.read_text().splitlines() if line.startswith('0::')]
        if len(entries) != 1 or not entries[0].startswith('/') or '..' in Path(entries[0]).parts:
            raise ValueError('unified membership missing')
        mounts = []
        for line in mountinfo.read_text().splitlines():
            fields, sep, tail = line.partition(' - ')
            if sep and tail.split()[0] == 'cgroup2':
                parts = fields.split()
                if _unescape(parts[3]) == '/':
                    mounts.append(Path(_unescape(parts[4])))
        if not mounts:
            raise ValueError('complete cgroup hierarchy unavailable')
        mount = mounts[0].resolve(strict=True)
        leaf = (mount / entries[0].lstrip('/')).resolve(strict=True)
        if not leaf.is_relative_to(mount):
            raise ValueError('membership escape')
        headrooms, maxima, highs, swaps = [], [], [], []
        for group in (leaf, *leaf.parents):
            if group == mount:
                break  # The root has no enclosing controller resource limit.
            current = _limit(group / 'memory.current')
            maximum = _limit(group / 'memory.max')
            high = _limit(group / 'memory.high')
            swap = _limit(group / 'memory.swap.max')
            if current is None:
                raise ValueError('usage unavailable')
            if maximum is not None:
                maxima.append(maximum)
                headrooms.append(max(0, maximum-current))
            if high is not None:
                highs.append(high)
            if swap is not None:
                swaps.append(swap)
        # The root may omit the controller's own resource files. It has no
        # enclosing cgroup limit; per-group observations above remain required.
        return {'headroom_bytes': min(headrooms) if headrooms else None,
                'memory_max_bytes': min(maxima) if maxima else None,
                'memory_high_bytes': min(highs) if highs else None,
                'swap_max_bytes': min(swaps) if swaps else None}
    except (OSError, ValueError, IndexError) as exc:
        raise ObservationUnavailable('cgroup_unknown') from exc


def host_available():
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                fields = line.split()
                if len(fields) == 3 and fields[2] == 'kB' and fields[1].isascii() and fields[1].isdecimal():
                    return int(fields[1])*1024
    except OSError:
        pass
    raise ObservationUnavailable('memavailable_unknown')


def bounded_scope_prefix(scope):
    """Preserve CPU limits and reuse only observed enclosing memory limits."""
    if not scope:  # Private test seam, used only inside the mandatory wrapper.
        return []
    target_nice = 15
    adjustment = target_nice - os.getpriority(os.PRIO_PROCESS, 0)
    if adjustment < 0:
        raise ObservationUnavailable('nice_target_unavailable')
    cpu = ['taskset', '-c', '0-15']
    adjusted_cpu = cpu + ['nice', '-n', str(adjustment)]
    scope_args = list(scope)
    old_cpu = cpu + ['nice', '-n', str(target_nice)]
    if scope_args[-len(old_cpu):] == old_cpu:
        del scope_args[-len(old_cpu):]
    # The topology launcher expands the original short-form scope. The
    # equivalent long flag marks this argv as already carrying its CPU prefix.
    if scope_args[:4] == ['systemd-run', '--user', '--scope', '-q']:
        scope_args[3] = '--quiet'
    full_scope = scope_args + adjusted_cpu
    try:
        observed = cgroup_observation()
    except ObservationUnavailable:
        return full_scope  # Establish limits instead of assuming inheritance.
    if (observed['memory_max_bytes'] is not None and observed['memory_max_bytes'] <= 8*2**30 and
            observed['memory_high_bytes'] is not None and observed['memory_high_bytes'] <= 6*2**30 and
            observed['swap_max_bytes'] == 0):
        return adjusted_cpu
    return full_scope
