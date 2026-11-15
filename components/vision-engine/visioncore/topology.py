# @module {"id":"vc.topology","layer":0,"stage":"L0","owner":"ARCH","deps":[],"inputs":"/proc/cpuinfo","outputs":"cpu id lists","invariants":["deterministic-given-host"],"anti_uses":["hard-coded cpu ranges"],"book":"HB-VC-a0-arch"}
"""@module vc.topology L0 - CPU topology from /proc/cpuinfo 'CPU part' (CDESIGN_CENG F4). Host MEASURED: X925 (0xd85)
= cpus 5-9,15-19, A725 (0xd87) = cpus 0-4,10-14 (clusters interleaved; never assume 0..9 / 10..19)."""
import os
import re

PART_X925 = 0xd85  # @anchor:vc.topology.part_x925
PART_A725 = 0xd87  # @anchor:vc.topology.part_a725


def parse_cpuinfo(text):  # @anchor:vc.topology.parse_cpuinfo
    """cpuinfo text -> {cpu_index: part_int}; cpus without a 'CPU part' line are omitted."""
    out, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"\s*processor\s*:\s*(\d+)\s*$", line)
        if m:
            cur = int(m.group(1))
            continue
        m = re.match(r"\s*CPU part\s*:\s*(0x[0-9a-fA-F]+|\d+)\s*$", line)
        if m and cur is not None:
            out[cur] = int(m.group(1), 0)
    return out


def cpus_by_part(text=None):  # @anchor:vc.topology.cpus_by_part
    """-> {part_int: sorted [cpu,...]}. text=None reads /proc/cpuinfo ({} when unreadable)."""
    if text is None:
        try:
            with open("/proc/cpuinfo", "r") as fh:
                text = fh.read()
        except OSError:
            return {}
    res = {}
    for cpu, part in sorted(parse_cpuinfo(text).items()):
        res.setdefault(part, []).append(cpu)
    return res


def big_cpus(text=None):  # @anchor:vc.topology.big_cpus
    """Cortex-X925 cpu ids (sorted); [] when the host has none (callers then do not pin)."""
    return list(cpus_by_part(text).get(PART_X925, []))


def little_cpus(text=None):  # @anchor:vc.topology.little_cpus
    return list(cpus_by_part(text).get(PART_A725, []))


def parse_cpu_list(spec):  # @anchor:vc.topology.parse_cpu_list
    """'5-9,15-19' -> [5..9,15..19] (sorted, unique). ValueError on malformed input."""
    out = set()
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            raise ValueError("empty cpu list item")
        lo, sep, hi = tok.partition("-")
        a = int(lo)
        b = int(hi) if sep else a
        if a < 0 or b < a:
            raise ValueError("bad cpu range " + tok)
        out.update(range(a, b + 1))
    return sorted(out)


def resolve_cpus(spec=None, text=None):  # @anchor:vc.topology.resolve_cpus
    """--cpus helper: 'big' -> X925 ids, 'little' -> A725 ids, 'auto'/None -> big ids, otherwise an explicit list.
    Result is intersected with the process's allowed set; [] means 'do not pin'."""
    if spec in (None, "", "auto", "big"):
        want = big_cpus(text)
    elif spec == "little":
        want = little_cpus(text)
    else:
        want = parse_cpu_list(spec)
    try:
        allowed = os.sched_getaffinity(0)
    except (AttributeError, OSError):
        return want
    return [c for c in want if c in allowed]


def pin(cpus):  # @anchor:vc.topology.pin
    """Pin the calling process to cpus; returns True when applied, False when cpus is empty or unsupported."""
    if not cpus:
        return False
    try:
        os.sched_setaffinity(0, set(cpus))
        return True
    except (AttributeError, OSError):
        return False
