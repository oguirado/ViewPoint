# @module {"id":"wa.learn.cgroup_memory","layer":2,"owner":"LEARN","deps":["wa.state.schema"],"book":"HB-WA-s4-learn"}
"""Whole-instance procfs PSS and cgroup v2 memory accounting (jq2b-j6-cgroup-memory).

Enforces invariants INV-J6-1 through INV-J6-4 and gate G-J6-1:
- INV-J6-1: Strict nonnegative byte integers for cgroup memory.current, memory.peak,
  and memory.swap.current; absent peak preserved as unsupported (None); strict
  nonnegative Pss quantities with unit kB and checked conversion to bytes; missing
  or unreadable data stays None with explicit status, never zero.
- INV-J6-2: Process identity includes (pid, stat starttime, boot_id); pre/post
  re-enumeration detects membership or incarnation changes; full process tree
  coverage (Chromium parent, renderer, GPU, utility, broker, descendants) with
  separate Wender, capsule, and shared overhead buckets.
- INV-J6-3: Never add PSS and cgroup charges, never add ancestor and child cgroup
  scopes, and never divide a shared browser by tab count; missing members mark the
  sample partial.
- INV-J6-4: No peak-reset subtraction or sampled-PSS substitution; PSS sampled peak
  is a lower-bound diagnostic only; lifetime cgroup peak requires exclusive ownership
  and freshness evidence; neither Python allocation peaks nor reserved frame bytes
  prove whole-instance RAM.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, fields
import hashlib
import json
import posixpath
import re
from typing import Any, Callable

import numpy as np

from wender.state import schema

INVARIANTS = ("INV-J6-1", "INV-J6-2", "INV-J6-3", "INV-J6-4")
GATE_ID = "G-J6-1"
CONDUCTOR_ADMISSION_BINDING = "AUSENTE"

CHROMIUM_ROLES = ("parent", "renderer", "gpu", "utility", "broker", "descendant")
GODOT_ROLES = ("parent", "engine", "gpu", "worker", "broker", "descendant")
OVERHEAD_SCOPES = ("wender", "capsule", "shared")
GPU_ACCOUNTING_MODES = (
    "drm_cgroup",
    "proc_smaps_only",
    "driver_sideband",
    "integrated_shared",
    "unaccounted",
    "unsupported",
    "AUSENTE",
)
SOURCE_POLICIES = ("cdp", "godot")

SAMPLE_FIELDS = (
    "sample_seq",
    "logical_tick",
    "monotonic_ns",
    "instance_id",
    "capsule_id",
    "processes",
    "cgroup_current_bytes",
    "cgroup_peak_bytes",
    "swap_current_bytes",
    "tree_complete",
    "gpu_accounting",
    "sample_skew_ns",
    "status",
)

HOLDINGS_PAUSE_KEYS = ("tick", "seq", "cmd", "args")
HOLDINGS_OUTCOME_KEYS = (
    "seq",
    "requested_tick",
    "applied_tick",
    "order_seq_id",
    "status",
    "reason",
    "state_sha256",
)
HOLDINGS_OUTCOME_STATUSES = frozenset({"accepted", "rejected", "proposed"})

_STRICT_BYTE_INT_RE = re.compile(r"\A(?:0|[1-9][0-9]*)\n?\Z")
_STRICT_PSS_LINE_RE = re.compile(r"\APss:[ \t]+(0|[1-9][0-9]*)[ \t]+kB\Z")
_HEX64_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_REASON_RE = re.compile(r"\A[A-Z][A-Z0-9_]*\Z")
_BOOT_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{4,128}\Z")
_VOLATILE_FORBIDDEN_KEYS = frozenset({
    "rgb",
    "pixels",
    "frame_bytes",
    "raw_frame",
    "raw_receipt",
    "hook_receipt",
    "hook_receipts",
    "raw_tree_dump",
    "tree_dump",
    "ax_tree_dump",
    "uitree_dump",
    "checkpoint_bytes",
    "opaque_checkpoint",
})


# @anchor:wa.learn.cgroup_memory.CgroupMemoryError
class CgroupMemoryError(ValueError):
    """Raised when procfs/cgroup memory inputs or accounting invariants are violated."""


def _strict_int(value: Any, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise CgroupMemoryError(f"{name} must be a strict int >= {minimum}")
    return value


def _nonempty_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise CgroupMemoryError(f"{name} must be a non-empty canonical string")
    value.encode("utf-8", errors="strict")
    return value


def _parse_strict_byte_quantity(raw: Any, field_name: str) -> int:
    if type(raw) is int:
        if raw < 0:
            raise CgroupMemoryError(f"{field_name} must be >= 0 bytes")
        return raw
    if not isinstance(raw, str):
        raise CgroupMemoryError(
            f"{field_name} must be a strict nonnegative byte integer or canonical decimal string"
        )
    if not _STRICT_BYTE_INT_RE.fullmatch(raw):
        raise CgroupMemoryError(f"malformed {field_name} byte quantity: {raw!r}")
    return int(raw[:-1] if raw.endswith("\n") else raw)


class _MappingRecordMixin(Mapping):
    """Provides Mapping access over dataclass fields and explicit alias properties."""

    _ALIAS_MAP: dict[str, str] = {}

    def _field_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in fields(self))

    def __getitem__(self, key: str) -> Any:
        if key in self._field_names():
            return getattr(self, key)
        if key in self._ALIAS_MAP:
            return getattr(self, self._ALIAS_MAP[key])
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._field_names())

    def __len__(self) -> int:
        return len(self._field_names())

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            val = getattr(self, f.name)
            if isinstance(val, _MappingRecordMixin):
                out[f.name] = val.to_dict()
            elif isinstance(val, tuple) and val and isinstance(val[0], _MappingRecordMixin):
                out[f.name] = [item.to_dict() for item in val]
            else:
                out[f.name] = val
        return out


# @anchor:wa.learn.cgroup_memory.CgroupMemoryScope
@dataclass(frozen=True)
class CgroupMemoryScope(_MappingRecordMixin):
    current_bytes: int | None
    peak_bytes: int | None
    swap_current_bytes: int | None
    peak_supported: bool
    swap_supported: bool
    status: str = "ok"

    _ALIAS_MAP = {
        "current": "current_bytes",
        "peak": "peak_bytes",
        "swap_current": "swap_current_bytes",
        "cgroup_current_bytes": "current_bytes",
        "cgroup_peak_bytes": "peak_bytes",
        "peak_status": "peak_status",
        "swap_status": "swap_status",
    }

    def __post_init__(self) -> None:
        if self.current_bytes is not None:
            _strict_int(self.current_bytes, "current_bytes", minimum=0)
        if self.peak_bytes is not None:
            _strict_int(self.peak_bytes, "peak_bytes", minimum=0)
        if self.swap_current_bytes is not None:
            _strict_int(self.swap_current_bytes, "swap_current_bytes", minimum=0)
        if type(self.peak_supported) is not bool or type(self.swap_supported) is not bool:
            raise CgroupMemoryError("peak_supported and swap_supported must be bool")
        _nonempty_str(self.status, "status")

    @property
    def current(self) -> int | None:
        return self.current_bytes

    @property
    def peak(self) -> int | None:
        return self.peak_bytes

    @property
    def swap_current(self) -> int | None:
        return self.swap_current_bytes

    @property
    def cgroup_current_bytes(self) -> int | None:
        return self.current_bytes

    @property
    def cgroup_peak_bytes(self) -> int | None:
        return self.peak_bytes

    @property
    def peak_status(self) -> str:
        return "ok" if self.peak_supported and self.peak_bytes is not None else "unsupported"

    @property
    def swap_status(self) -> str:
        return "ok" if self.swap_supported and self.swap_current_bytes is not None else "unsupported"


# @anchor:wa.learn.cgroup_memory.ProcessIdentity
@dataclass(frozen=True)
class ProcessIdentity(_MappingRecordMixin):
    pid: int
    starttime: int
    boot_id: str

    _ALIAS_MAP = {
        "starttime_ticks": "starttime",
    }

    def __post_init__(self) -> None:
        _strict_int(self.pid, "pid", minimum=1)
        _strict_int(self.starttime, "starttime", minimum=0)
        _nonempty_str(self.boot_id, "boot_id")

    @property
    def starttime_ticks(self) -> int:
        return self.starttime


# @anchor:wa.learn.cgroup_memory.ProcessSample
@dataclass(frozen=True)
class ProcessSample(_MappingRecordMixin):
    pid: int
    starttime: int | None
    boot_id: str | None
    role: str
    scope: str
    pss_bytes: int | None
    status: str
    ppid: int | None = None
    comm: str | None = None

    _ALIAS_MAP = {
        "starttime_ticks": "starttime",
        "pss_kb": "pss_kb",
        "identity": "identity",
    }

    def __post_init__(self) -> None:
        _strict_int(self.pid, "pid", minimum=1)
        if self.starttime is not None:
            _strict_int(self.starttime, "starttime", minimum=0)
        if self.boot_id is not None:
            _nonempty_str(self.boot_id, "boot_id")
        _nonempty_str(self.role, "role")
        _nonempty_str(self.scope, "scope")
        if self.pss_bytes is not None:
            _strict_int(self.pss_bytes, "pss_bytes", minimum=0)
            if self.pss_bytes % 1024 != 0:
                raise CgroupMemoryError("pss_bytes must be an exact multiple of 1024 bytes (from kB)")
        if self.status == "ok" and (
            self.pss_bytes is None or self.starttime is None or self.boot_id is None
        ):
            raise CgroupMemoryError("process sample with status 'ok' cannot have null identity or pss_bytes")
        _nonempty_str(self.status, "status")
        if self.ppid is not None:
            _strict_int(self.ppid, "ppid", minimum=0)

    @property
    def starttime_ticks(self) -> int | None:
        return self.starttime

    @property
    def pss_kb(self) -> int | None:
        return None if self.pss_bytes is None else self.pss_bytes // 1024

    @property
    def identity(self) -> ProcessIdentity | None:
        if self.starttime is None or self.boot_id is None:
            return None
        return ProcessIdentity(self.pid, self.starttime, self.boot_id)


# @anchor:wa.learn.cgroup_memory.OverheadAccounting
@dataclass(frozen=True)
class OverheadAccounting(_MappingRecordMixin):
    wender_bytes: int | None
    capsule_bytes: int | None
    shared_bytes: int | None
    status: str = "ok"

    _ALIAS_MAP = {
        "wender": "wender_bytes",
        "capsule": "capsule_bytes",
        "shared": "shared_bytes",
    }

    def __post_init__(self) -> None:
        for field_name in ("wender_bytes", "capsule_bytes", "shared_bytes"):
            val = getattr(self, field_name)
            if val is not None:
                _strict_int(val, field_name, minimum=0)
        _nonempty_str(self.status, "status")

    @property
    def wender(self) -> int | None:
        return self.wender_bytes

    @property
    def capsule(self) -> int | None:
        return self.capsule_bytes

    @property
    def shared(self) -> int | None:
        return self.shared_bytes


# @anchor:wa.learn.cgroup_memory.MemorySample
@dataclass(frozen=True)
class MemorySample(_MappingRecordMixin):
    sample_seq: int
    logical_tick: int
    monotonic_ns: int
    instance_id: str
    capsule_id: str | None
    processes: tuple[ProcessSample, ...]
    cgroup_current_bytes: int | None
    cgroup_peak_bytes: int | None
    swap_current_bytes: int | None
    tree_complete: bool
    gpu_accounting: str
    sample_skew_ns: int
    status: str
    source_policy: str = "cdp"
    pss_total_bytes: int | None = None
    overhead: OverheadAccounting | None = None
    cgroup_dir: str | None = None
    violations: tuple[str, ...] = ()

    _ALIAS_MAP = {
        "wender_overhead_bytes": "wender_overhead_bytes",
        "capsule_overhead_bytes": "capsule_overhead_bytes",
        "shared_overhead_bytes": "shared_overhead_bytes",
    }

    def __post_init__(self) -> None:
        _strict_int(self.sample_seq, "sample_seq", minimum=0)
        _strict_int(self.logical_tick, "logical_tick", minimum=0)
        _strict_int(self.monotonic_ns, "monotonic_ns", minimum=0)
        _nonempty_str(self.instance_id, "instance_id")
        if self.capsule_id is not None:
            _nonempty_str(self.capsule_id, "capsule_id")
        if not isinstance(self.processes, tuple) or any(
            not isinstance(p, ProcessSample) for p in self.processes
        ):
            raise CgroupMemoryError("processes must be a tuple of ProcessSample records")
        for field_name in ("cgroup_current_bytes", "cgroup_peak_bytes", "swap_current_bytes"):
            val = getattr(self, field_name)
            if val is not None:
                _strict_int(val, field_name, minimum=0)
        if type(self.tree_complete) is not bool:
            raise CgroupMemoryError("tree_complete must be bool")
        if self.gpu_accounting not in GPU_ACCOUNTING_MODES:
            raise CgroupMemoryError(f"unsupported gpu_accounting mode: {self.gpu_accounting!r}")
        _strict_int(self.sample_skew_ns, "sample_skew_ns", minimum=0)
        _nonempty_str(self.status, "status")
        if self.source_policy not in SOURCE_POLICIES:
            raise CgroupMemoryError(f"unsupported source_policy: {self.source_policy!r}")
        if self.pss_total_bytes is not None:
            _strict_int(self.pss_total_bytes, "pss_total_bytes", minimum=0)
        if not self.tree_complete and self.status == "ok":
            raise CgroupMemoryError("incomplete process tree sample cannot report status 'ok'")

    @property
    def wender_overhead_bytes(self) -> int | None:
        return None if self.overhead is None else self.overhead.wender_bytes

    @property
    def capsule_overhead_bytes(self) -> int | None:
        return None if self.overhead is None else self.overhead.capsule_bytes

    @property
    def shared_overhead_bytes(self) -> int | None:
        return None if self.overhead is None else self.overhead.shared_bytes


# @anchor:wa.learn.cgroup_memory.MemoryBudgetLimits
@dataclass(frozen=True)
class MemoryBudgetLimits(_MappingRecordMixin):
    max_cgroup_current_bytes: int
    max_cgroup_peak_bytes: int
    max_swap_current_bytes: int
    max_sample_skew_ns: int
    max_pss_lower_bound_bytes: int | None = None

    def __post_init__(self) -> None:
        _strict_int(self.max_cgroup_current_bytes, "max_cgroup_current_bytes", minimum=1)
        _strict_int(self.max_cgroup_peak_bytes, "max_cgroup_peak_bytes", minimum=1)
        _strict_int(self.max_swap_current_bytes, "max_swap_current_bytes", minimum=0)
        _strict_int(self.max_sample_skew_ns, "max_sample_skew_ns", minimum=0)
        if self.max_pss_lower_bound_bytes is not None:
            _strict_int(self.max_pss_lower_bound_bytes, "max_pss_lower_bound_bytes", minimum=1)


# @anchor:wa.learn.cgroup_memory.PeakEvaluation
@dataclass(frozen=True)
class PeakEvaluation(_MappingRecordMixin):
    cgroup_peak_bytes: int | None
    pss_sampled_peak_bytes: int | None
    pss_peak_classification: str
    lifetime_peak_admissible: bool
    exclusive_ownership: bool
    freshness_evidence: bool
    status: str
    violations: tuple[str, ...] = ()


# @anchor:wa.learn.cgroup_memory.GateReceipt
@dataclass(frozen=True)
class GateReceipt(_MappingRecordMixin):
    gate_id: str
    status: str
    passed: bool
    invariants: dict[str, bool]
    violations: tuple[str, ...]
    conductor_admission_binding: str
    host_admitted: bool
    retail_migration_blocked: bool
    evidence: dict[str, Any]


# @anchor:wa.learn.cgroup_memory.parse_cgroup_memory
def parse_cgroup_memory(
    current: Any,
    peak: Any = None,
    *,
    swap_current: Any = None,
) -> CgroupMemoryScope:
    """Parse cgroup v2 memory.current, optional memory.peak, and keyword memory.swap.current.

    Requires strict nonnegative byte integers. Preserves absent peak (None) as
    unsupported (peak_bytes=None, peak_supported=False) and rejects malformed
    quantities rather than coercing or substituting.
    """
    if current is None:
        raise CgroupMemoryError("memory.current is required and cannot be None")
    current_bytes = _parse_strict_byte_quantity(current, "memory.current")

    if peak is None:
        peak_bytes = None
        peak_supported = False
    else:
        peak_bytes = _parse_strict_byte_quantity(peak, "memory.peak")
        peak_supported = True

    if swap_current is None:
        swap_bytes = None
        swap_supported = False
    else:
        swap_bytes = _parse_strict_byte_quantity(swap_current, "memory.swap.current")
        swap_supported = True

    status = "ok" if peak_supported else "peak_unsupported"
    return CgroupMemoryScope(
        current_bytes=current_bytes,
        peak_bytes=peak_bytes,
        swap_current_bytes=swap_bytes,
        peak_supported=peak_supported,
        swap_supported=swap_supported,
        status=status,
    )


# @anchor:wa.learn.cgroup_memory.parse_smaps_rollup_pss
def parse_smaps_rollup_pss(text: str | None) -> tuple[int | None, str]:
    """Parse `/proc/<pid>/smaps_rollup` text for `Pss: <N> kB` and convert to bytes.

    - Missing or None text returns `(None, "missing_smaps_rollup")` (never 0).
    - Text without a `Pss:` line returns `(None, "missing_pss")` (never 0).
    - Malformed `Pss:` quantities, non-kB units, negative numbers, floats, or
      conflicting duplicate `Pss:` lines raise `CgroupMemoryError`.
    """
    if text is None:
        return None, "missing_smaps_rollup"
    if not isinstance(text, str):
        raise CgroupMemoryError("smaps_rollup content must be str or None")
    if "\r" in text or "\x00" in text:
        raise CgroupMemoryError("malformed smaps_rollup line endings or NUL byte")

    found_kb: int | None = None
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped.startswith("Pss:"):
            continue
        match = _STRICT_PSS_LINE_RE.fullmatch(raw_line)
        if not match:
            raise CgroupMemoryError(f"malformed Pss line (requires strict nonnegative integer and kB unit): {raw_line!r}")
        kb_value = int(match.group(1))
        if found_kb is not None and found_kb != kb_value:
            raise CgroupMemoryError("conflicting duplicate Pss lines in smaps_rollup")
        found_kb = kb_value

    if found_kb is None:
        return None, "missing_pss"

    pss_bytes = found_kb * 1024
    if pss_bytes // 1024 != found_kb or pss_bytes < 0:
        raise CgroupMemoryError("checked kB-to-bytes conversion overflow or error")
    return pss_bytes, "ok"


# @anchor:wa.learn.cgroup_memory.parse_pss_kb
def parse_pss_kb(text: str | None, *, require_present: bool = False) -> int | None:
    """Parse `Pss: <N> kB` from smaps_rollup text and return exact bytes (or None if absent)."""
    pss_bytes, status = parse_smaps_rollup_pss(text)
    if pss_bytes is None and require_present:
        raise CgroupMemoryError(f"Pss quantity is absent ({status})")
    return pss_bytes


# @anchor:wa.learn.cgroup_memory.parse_boot_id
def parse_boot_id(text: str | None) -> str:
    """Validate and normalize `/proc/sys/kernel/random/boot_id` text."""
    if text is None or not isinstance(text, str):
        raise CgroupMemoryError("boot_id is AUSENTE or not a string")
    if "\r" in text or "\x00" in text:
        raise CgroupMemoryError("malformed boot_id control characters")
    lines = text.splitlines()
    if len(lines) != 1:
        raise CgroupMemoryError("boot_id must be a single line")
    token = lines[0]
    if not _BOOT_ID_RE.fullmatch(token):
        raise CgroupMemoryError(f"malformed boot_id token: {token!r}")
    return token


# @anchor:wa.learn.cgroup_memory.parse_proc_stat
def parse_proc_stat(
    stat_input: str | Mapping[str, Any] | None,
    *,
    expected_pid: int | None = None,
) -> dict[str, Any] | None:
    """Parse `/proc/<pid>/stat` text (or structured stat mapping) for pid, comm, ppid, starttime.

    Returns None if `stat_input` is None (unreadable/exited process).
    Raises `CgroupMemoryError` if `stat_input` is malformed or mismatches `expected_pid`.
    """
    if stat_input is None:
        return None
    if expected_pid is not None:
        _strict_int(expected_pid, "expected_pid", minimum=1)

    if isinstance(stat_input, Mapping):
        pid = _strict_int(stat_input.get("pid"), "stat.pid", minimum=1)
        if expected_pid is not None and pid != expected_pid:
            raise CgroupMemoryError(f"stat pid {pid} does not match expected pid {expected_pid}")
        ppid = _strict_int(stat_input.get("ppid", 0), "stat.ppid", minimum=0)
        raw_st = stat_input.get("starttime", stat_input.get("starttime_ticks"))
        starttime = _strict_int(raw_st, "stat.starttime", minimum=0)
        comm = _nonempty_str(stat_input.get("comm", "proc"), "stat.comm")
        state = _nonempty_str(stat_input.get("state", "S"), "stat.state")
        return {
            "pid": pid,
            "comm": comm,
            "state": state,
            "ppid": ppid,
            "starttime": starttime,
        }

    if not isinstance(stat_input, str):
        raise CgroupMemoryError("proc stat input must be str, Mapping, or None")
    if "\r" in stat_input or "\x00" in stat_input:
        raise CgroupMemoryError("malformed proc stat line endings or NUL")
    lines = stat_input.splitlines()
    if len(lines) != 1:
        raise CgroupMemoryError("proc stat must be a single line")
    line = lines[0]
    lparen = line.find("(")
    rparen = line.rfind(")")
    if lparen <= 0 or rparen <= lparen + 1 or line[lparen - 1] != " ":
        raise CgroupMemoryError(f"malformed /proc/<pid>/stat comm delimiters: {line!r}")

    pid_str = line[: lparen - 1]
    if not re.fullmatch(r"[1-9][0-9]*", pid_str):
        raise CgroupMemoryError(f"malformed /proc/<pid>/stat pid: {pid_str!r}")
    pid = int(pid_str)
    if expected_pid is not None and pid != expected_pid:
        raise CgroupMemoryError(f"stat pid {pid} does not match expected pid {expected_pid}")

    comm = line[lparen + 1 : rparen]
    after = line[rparen + 1 :]
    if not after.startswith(" "):
        raise CgroupMemoryError("malformed /proc/<pid>/stat after comm")
    rest = after.split()
    # In Linux /proc/<pid>/stat, field 3 is rest[0] (state), field 4 is rest[1] (ppid),
    # and field 22 is rest[19] (starttime in clock ticks since boot).
    if len(rest) < 20:
        raise CgroupMemoryError(
            f"/proc/<pid>/stat requires at least 22 fields (found {len(rest) + 2})"
        )
    state = rest[0]
    if not re.fullmatch(r"[A-Za-z]", state):
        raise CgroupMemoryError(f"malformed /proc/<pid>/stat state: {state!r}")
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)", rest[1]):
        raise CgroupMemoryError(f"malformed /proc/<pid>/stat ppid: {rest[1]!r}")
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)", rest[19]):
        raise CgroupMemoryError(f"malformed /proc/<pid>/stat starttime: {rest[19]!r}")
    ppid = int(rest[1])
    starttime = int(rest[19])
    return {
        "pid": pid,
        "comm": comm,
        "state": state,
        "ppid": ppid,
        "starttime": starttime,
    }


def _normalize_strict_abs_path(path: Any, name: str) -> str:
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        raise CgroupMemoryError(f"{name} must be an absolute POSIX path")
    parts = path.split("/")
    if any(p in ("..", ".") for p in parts if p):
        raise CgroupMemoryError(f"{name} must not contain '.' or '..' traversal segments")
    normed = posixpath.normpath(path)
    if not normed.startswith("/"):
        raise CgroupMemoryError(f"{name} normalization failed")
    return normed


def _parse_cgroup2_mounts(mountinfo: str | Mapping[str, Any] | Sequence[Any] | None) -> list[str]:
    if mountinfo is None:
        raise CgroupMemoryError("mountinfo is AUSENTE; verified cgroup2 mount membership is required")
    mounts: list[str] = []
    if isinstance(mountinfo, str):
        for raw_line in mountinfo.splitlines():
            if not raw_line.strip():
                continue
            if " - " not in raw_line:
                raise CgroupMemoryError(f"malformed mountinfo line: {raw_line!r}")
            left, right = raw_line.split(" - ", 1)
            left_parts = left.split()
            right_parts = right.split()
            if len(left_parts) < 5 or len(right_parts) < 2:
                raise CgroupMemoryError(f"malformed mountinfo fields: {raw_line!r}")
            mount_point = _normalize_strict_abs_path(left_parts[4], "mount_point")
            fstype = right_parts[0]
            if fstype == "cgroup2":
                mounts.append(mount_point)
    elif isinstance(mountinfo, Mapping):
        if mountinfo.get("fstype") == "cgroup2" and "mount_point" in mountinfo:
            mounts.append(_normalize_strict_abs_path(mountinfo["mount_point"], "mount_point"))
        else:
            for mp, fstype in mountinfo.items():
                if fstype == "cgroup2":
                    mounts.append(_normalize_strict_abs_path(mp, "mount_point"))
    elif isinstance(mountinfo, Sequence):
        for item in mountinfo:
            if isinstance(item, Mapping) and item.get("fstype") == "cgroup2":
                mounts.append(_normalize_strict_abs_path(item.get("mount_point"), "mount_point"))
    else:
        raise CgroupMemoryError("unsupported mountinfo representation")

    if not mounts:
        raise CgroupMemoryError("no active cgroup2 mount found in mountinfo")
    return sorted(set(mounts), key=len, reverse=True)


# @anchor:wa.learn.cgroup_memory.resolve_trusted_cgroup_dir
def resolve_trusted_cgroup_dir(
    launcher_metadata: Mapping[str, Any] | None,
    *,
    mountinfo: str | Mapping[str, Any] | Sequence[Any] | None = None,
    cgroup_procs: str | Sequence[int] | None = None,
    proc_cgroup_by_pid: Mapping[int, str] | None = None,
) -> str:
    """Resolve and verify the live cgroup v2 directory from trusted launcher metadata.

    Never guesses a cgroup path from PID. Verifies that the path resides under a
    mounted `cgroup2` filesystem and that declared PIDs match the cgroup's membership.
    """
    if not isinstance(launcher_metadata, Mapping):
        raise CgroupMemoryError("launcher_metadata must be a mapping from a trusted launcher")
    if launcher_metadata.get("trusted", True) is not True:
        raise CgroupMemoryError("launcher_metadata is not trusted")
    if (
        launcher_metadata.get("guessed_from_pid") is True
        or launcher_metadata.get("discovery_mode") == "pid_guess"
        or launcher_metadata.get("source") == "pid_guess"
    ):
        raise CgroupMemoryError("never guess a cgroup path from PID")

    raw_dir = launcher_metadata.get("cgroup_dir", launcher_metadata.get("live_dir"))
    if raw_dir is None:
        raise CgroupMemoryError(
            "cgroup_dir is AUSENTE in launcher_metadata; never guess a cgroup path from PID"
        )
    cgroup_dir = _normalize_strict_abs_path(raw_dir, "cgroup_dir")

    effective_mountinfo = mountinfo if mountinfo is not None else launcher_metadata.get("mountinfo")
    cgroup2_mounts = _parse_cgroup2_mounts(effective_mountinfo)

    matched_mount: str | None = None
    for mp in cgroup2_mounts:
        if cgroup_dir == mp or cgroup_dir.startswith(mp.rstrip("/") + "/"):
            matched_mount = mp
            break
    if matched_mount is None:
        raise CgroupMemoryError(
            f"cgroup_dir {cgroup_dir!r} is not under any verified cgroup2 mount {cgroup2_mounts!r}"
        )

    rel_cgroup = cgroup_dir[len(matched_mount.rstrip("/")) :] or "/"
    if not rel_cgroup.startswith("/"):
        rel_cgroup = "/" + rel_cgroup

    required_pids: list[int] = []
    if "root_pid" in launcher_metadata and launcher_metadata["root_pid"] is not None:
        required_pids.append(_strict_int(launcher_metadata["root_pid"], "root_pid", minimum=1))
    if "expected_pids" in launcher_metadata and launcher_metadata["expected_pids"] is not None:
        for p in launcher_metadata["expected_pids"]:
            required_pids.append(_strict_int(p, "expected_pid", minimum=1))

    effective_procs = cgroup_procs if cgroup_procs is not None else launcher_metadata.get("cgroup_procs")
    if effective_procs is None and proc_cgroup_by_pid is None:
        raise CgroupMemoryError("verified cgroup mount membership requires cgroup_procs or proc_cgroup_by_pid")

    if effective_procs is not None:
        member_pids: set[int] = set()
        if isinstance(effective_procs, str):
            for line in effective_procs.splitlines():
                if not line.strip():
                    continue
                if not re.fullmatch(r"[1-9][0-9]*", line.strip()):
                    raise CgroupMemoryError(f"malformed PID in cgroup.procs: {line!r}")
                member_pids.add(int(line.strip()))
        elif isinstance(effective_procs, Sequence):
            for item in effective_procs:
                member_pids.add(_strict_int(item, "cgroup_procs pid", minimum=1))
        else:
            raise CgroupMemoryError("unsupported cgroup_procs format")

        for req_pid in required_pids:
            if req_pid not in member_pids:
                raise CgroupMemoryError(
                    f"required process {req_pid} is not a member of cgroup.procs for {cgroup_dir}"
                )

    if proc_cgroup_by_pid is not None:
        if not isinstance(proc_cgroup_by_pid, Mapping):
            raise CgroupMemoryError("proc_cgroup_by_pid must be a mapping")
        check_pids = required_pids or list(proc_cgroup_by_pid.keys())
        for pid in check_pids:
            _strict_int(pid, "pid", minimum=1)
            cg_text = proc_cgroup_by_pid.get(pid)
            if not isinstance(cg_text, str) or not cg_text.strip():
                raise CgroupMemoryError(f"missing /proc/{pid}/cgroup text for membership check")
            v2_paths = [
                line.split("::", 1)[1].strip()
                for line in cg_text.splitlines()
                if line.startswith("0::")
            ]
            if len(v2_paths) != 1 or v2_paths[0] != rel_cgroup:
                raise CgroupMemoryError(
                    f"/proc/{pid}/cgroup v2 path {v2_paths!r} does not match expected {rel_cgroup!r}"
                )

    return cgroup_dir


# @anchor:wa.learn.cgroup_memory.enumerate_process_identities
def enumerate_process_identities(
    proc_table: Mapping[int, Mapping[str, Any] | str | None],
    *,
    boot_id_text: str,
) -> tuple[dict[int, ProcessIdentity], list[str]]:
    """Enumerate `(pid, stat.starttime, boot_id)` across a procfs snapshot."""
    boot_id = parse_boot_id(boot_id_text)
    if not isinstance(proc_table, Mapping):
        raise CgroupMemoryError("proc_table must be a mapping keyed by pid")
    identities: dict[int, ProcessIdentity] = {}
    issues: list[str] = []
    for raw_pid in sorted(proc_table.keys()):
        pid = _strict_int(raw_pid, "pid", minimum=1)
        entry = proc_table[raw_pid]
        stat_raw = entry.get("stat") if isinstance(entry, Mapping) and "stat" in entry else entry
        parsed = parse_proc_stat(stat_raw, expected_pid=pid)
        if parsed is None:
            issues.append(f"missing_stat:{pid}")
            continue
        entry_boot = (
            parse_boot_id(entry["boot_id"])
            if isinstance(entry, Mapping) and entry.get("boot_id") is not None
            else boot_id
        )
        identities[pid] = ProcessIdentity(
            pid=pid,
            starttime=parsed["starttime"],
            boot_id=entry_boot,
        )
    return identities, issues


# @anchor:wa.learn.cgroup_memory.forbid_invalid_ram_arithmetic
def forbid_invalid_ram_arithmetic(
    *,
    add_pss_and_cgroup: bool = False,
    ancestor_cgroup_path: str | None = None,
    child_cgroup_path: str | None = None,
    shared_browser_bytes: int | None = None,
    tab_count: int | None = None,
    peak_reset_subtraction: bool = False,
    substitute_pss_for_peak: bool = False,
) -> None:
    """Enforce INV-J6-3 and INV-J6-4 prohibitions on invalid memory arithmetic."""
    if add_pss_and_cgroup:
        raise CgroupMemoryError("INV-J6-3 violation: never add PSS and cgroup charges")
    if ancestor_cgroup_path is not None and child_cgroup_path is not None:
        anc = _normalize_strict_abs_path(ancestor_cgroup_path, "ancestor_cgroup_path")
        chd = _normalize_strict_abs_path(child_cgroup_path, "child_cgroup_path")
        if anc == chd or chd.startswith(anc.rstrip("/") + "/") or anc.startswith(chd.rstrip("/") + "/"):
            raise CgroupMemoryError(
                f"INV-J6-3 violation: never add ancestor ({anc}) and child ({chd}) cgroup scopes"
            )
    if shared_browser_bytes is not None or tab_count is not None:
        raise CgroupMemoryError(
            "INV-J6-3 violation: never divide a shared browser by tab count"
        )
    if peak_reset_subtraction:
        raise CgroupMemoryError(
            "INV-J6-4 violation: never compute cgroup peak by peak-reset baseline subtraction"
        )
    if substitute_pss_for_peak:
        raise CgroupMemoryError(
            "INV-J6-4 violation: never substitute sampled PSS for absent cgroup memory.peak"
        )


def _compute_overhead(
    overhead_smaps: Mapping[str, str | int | None] | None,
) -> tuple[OverheadAccounting, list[str]]:
    if overhead_smaps is None:
        return OverheadAccounting(None, None, None, status="ok"), []
    if not isinstance(overhead_smaps, Mapping):
        raise CgroupMemoryError("overhead_smaps must be a mapping")
    unknown = set(overhead_smaps.keys()) - set(OVERHEAD_SCOPES)
    if unknown:
        raise CgroupMemoryError(f"unknown overhead scopes: {sorted(unknown)!r}")
    values: dict[str, int | None] = {}
    issues: list[str] = []
    for scope_name in OVERHEAD_SCOPES:
        if scope_name not in overhead_smaps:
            values[scope_name] = None
            continue
        raw = overhead_smaps[scope_name]
        if raw is None:
            values[scope_name] = None
            issues.append(f"missing_overhead_{scope_name}")
        elif type(raw) is int:
            values[scope_name] = _strict_int(raw, f"overhead.{scope_name}", minimum=0)
        elif isinstance(raw, str):
            pss_b, st = parse_smaps_rollup_pss(raw)
            values[scope_name] = pss_b
            if pss_b is None:
                issues.append(f"overhead_{scope_name}_{st}")
        else:
            raise CgroupMemoryError(f"invalid overhead entry for {scope_name!r}")
    status = "ok" if not issues else "partial_overhead"
    return (
        OverheadAccounting(
            wender_bytes=values["wender"],
            capsule_bytes=values["capsule"],
            shared_bytes=values["shared"],
            status=status,
        ),
        issues,
    )


# @anchor:wa.learn.cgroup_memory.sample_instance_memory
def sample_instance_memory(
    *,
    sample_seq: int,
    logical_tick: int,
    instance_id: str,
    capsule_id: str | None = None,
    launcher_metadata: Mapping[str, Any],
    mountinfo: str | Mapping[str, Any] | Sequence[Any] | None = None,
    boot_id_text: str,
    pre_proc_table: Mapping[int, Mapping[str, Any]],
    post_proc_table: Mapping[int, Mapping[str, Any]] | None = None,
    smaps_by_pid: Mapping[int, str | None],
    cgroup_files: Mapping[str, Any],
    clock: Callable[[], int],
    max_skew_ns: int | None = None,
    expected_pids: Sequence[int] | None = None,
    expected_roles: Sequence[str] | None = None,
    source_policy: str = "cdp",
    source_descriptor: Mapping[str, Any] | None = None,
    gpu_accounting: str = "drm_cgroup",
    overhead_smaps: Mapping[str, str | int | None] | None = None,
) -> MemorySample:
    """Take a whole-instance procfs + cgroup v2 memory sample with pre/post re-enumeration."""
    _strict_int(sample_seq, "sample_seq", minimum=0)
    _strict_int(logical_tick, "logical_tick", minimum=0)
    _nonempty_str(instance_id, "instance_id")
    if capsule_id is not None:
        _nonempty_str(capsule_id, "capsule_id")
    if source_policy not in SOURCE_POLICIES:
        raise CgroupMemoryError(f"unsupported source_policy: {source_policy!r}")
    if gpu_accounting not in GPU_ACCOUNTING_MODES:
        raise CgroupMemoryError(f"unsupported gpu_accounting: {gpu_accounting!r}")
    if not callable(clock):
        raise CgroupMemoryError("an injected monotonic clock callable is required")

    # Verify source policy vs descriptor without relabeling one source as the other.
    if source_descriptor is not None:
        if not isinstance(source_descriptor, Mapping):
            raise CgroupMemoryError("source_descriptor must be a mapping")
        src = source_descriptor.get("source")
        src_kind = src.get("kind") if isinstance(src, Mapping) else source_descriptor.get("kind")
        if source_policy == "cdp" and src_kind not in ("cdp", "cdp_tab", "capsule", "xvfb_screen"):
            raise CgroupMemoryError(
                f"source_policy 'cdp' cannot relabel source kind {src_kind!r}"
            )
        if source_policy == "godot" and src_kind not in ("godot", "godot_window"):
            raise CgroupMemoryError(
                f"source_policy 'godot' cannot relabel source kind {src_kind!r}"
            )

    cgroup_dir = resolve_trusted_cgroup_dir(launcher_metadata, mountinfo=mountinfo)
    boot_id = parse_boot_id(boot_id_text)

    start_ns = _strict_int(clock(), "clock_start_ns", minimum=0)
    pre_ids, pre_issues = enumerate_process_identities(pre_proc_table, boot_id_text=boot_id_text)

    violations: list[str] = list(pre_issues)
    tree_complete = len(pre_issues) == 0

    if not isinstance(smaps_by_pid, Mapping):
        raise CgroupMemoryError("smaps_by_pid must be a mapping")

    declared_pids = (
        sorted({_strict_int(p, "expected_pid", minimum=1) for p in expected_pids})
        if expected_pids is not None
        else sorted(pre_proc_table.keys())
    )
    if not declared_pids:
        tree_complete = False
        violations.append("empty_process_tree")

    for req_pid in declared_pids:
        if req_pid not in pre_proc_table:
            tree_complete = False
            violations.append(f"missing_member:{req_pid}")

    all_pids = sorted(set(declared_pids) | {int(k) for k in pre_proc_table.keys()})
    proc_samples: list[ProcessSample] = []
    observed_roles: set[str] = set()

    for pid in all_pids:
        entry = pre_proc_table.get(pid)
        if entry is None:
            proc_samples.append(
                ProcessSample(
                    pid=pid,
                    starttime=None,
                    boot_id=None,
                    role="missing",
                    scope="instance",
                    pss_bytes=None,
                    status="missing_member",
                )
            )
            tree_complete = False
            continue

        if not isinstance(entry, Mapping):
            raise CgroupMemoryError(f"proc_table[{pid}] must be a mapping")

        role = _nonempty_str(entry.get("role", "descendant"), f"proc_table[{pid}].role")
        scope = _nonempty_str(entry.get("scope", "instance"), f"proc_table[{pid}].scope")
        stat_info = parse_proc_stat(entry.get("stat"), expected_pid=pid)
        entry_boot = (
            parse_boot_id(entry["boot_id"])
            if entry.get("boot_id") is not None
            else boot_id
        )
        if entry_boot != boot_id:
            tree_complete = False
            violations.append(f"boot_id_mismatch:{pid}")

        raw_smaps = smaps_by_pid.get(pid)
        pss_bytes, pss_status = parse_smaps_rollup_pss(raw_smaps)
        if stat_info is None:
            p_status = "missing_stat"
            tree_complete = False
        elif pss_bytes is None:
            p_status = pss_status
            tree_complete = False
            violations.append(f"{pss_status}:{pid}")
        else:
            p_status = "ok"
            if scope == "instance":
                observed_roles.add(role)

        proc_samples.append(
            ProcessSample(
                pid=pid,
                starttime=None if stat_info is None else stat_info["starttime"],
                boot_id=None if stat_info is None else entry_boot,
                role=role,
                scope=scope,
                pss_bytes=pss_bytes,
                status=p_status,
                ppid=None if stat_info is None else stat_info["ppid"],
                comm=None if stat_info is None else stat_info["comm"],
            )
        )

    required_roles = (
        tuple(expected_roles)
        if expected_roles is not None
        else (CHROMIUM_ROLES if source_policy == "cdp" else ("parent",))
    )
    for req_role in required_roles:
        if req_role not in observed_roles:
            tree_complete = False
            violations.append(f"missing_role:{req_role}")

    # Parse cgroup files (Missing/unreadable memory.current stays None with status, never 0).
    if not isinstance(cgroup_files, Mapping):
        raise CgroupMemoryError("cgroup_files must be a mapping")
    raw_current = cgroup_files.get("memory.current", cgroup_files.get("current"))
    raw_peak = cgroup_files.get("memory.peak", cgroup_files.get("peak"))
    raw_swap = cgroup_files.get("memory.swap.current", cgroup_files.get("swap_current"))

    if raw_current is None:
        cg_current_bytes = None
        cg_peak_bytes = (
            None if raw_peak is None else _parse_strict_byte_quantity(raw_peak, "memory.peak")
        )
        cg_swap_bytes = (
            None
            if raw_swap is None
            else _parse_strict_byte_quantity(raw_swap, "memory.swap.current")
        )
        violations.append("missing_cgroup_current")
    else:
        cg_scope = parse_cgroup_memory(raw_current, raw_peak, swap_current=raw_swap)
        cg_current_bytes = cg_scope.current_bytes
        cg_peak_bytes = cg_scope.peak_bytes
        cg_swap_bytes = cg_scope.swap_current_bytes

    effective_post = pre_proc_table if post_proc_table is None else post_proc_table
    post_ids, post_issues = enumerate_process_identities(
        effective_post, boot_id_text=boot_id_text
    )
    if post_issues:
        tree_complete = False
        violations.extend(post_issues)

    if set(pre_ids.keys()) != set(post_ids.keys()):
        tree_complete = False
        violations.append("membership_changed")
    else:
        for pid, pre_ident in pre_ids.items():
            post_ident = post_ids.get(pid)
            if post_ident != pre_ident:
                tree_complete = False
                violations.append(f"incarnation_changed:{pid}")

    overhead, overhead_issues = _compute_overhead(overhead_smaps)
    if overhead_issues:
        violations.extend(overhead_issues)

    end_ns = _strict_int(clock(), "clock_end_ns", minimum=0)
    if end_ns < start_ns:
        raise CgroupMemoryError("monotonic clock moved backwards during memory sample")
    skew_ns = end_ns - start_ns
    if max_skew_ns is not None:
        _strict_int(max_skew_ns, "max_skew_ns", minimum=0)
        if skew_ns > max_skew_ns:
            violations.append("sample_skew_exceeded")

    if gpu_accounting in ("unaccounted", "unsupported", "AUSENTE"):
        violations.append(f"gpu_accounting_{gpu_accounting}")

    instance_procs = [p for p in proc_samples if p.scope == "instance"]
    if (
        tree_complete
        and instance_procs
        and all(p.pss_bytes is not None for p in instance_procs)
    ):
        pss_total: int | None = sum(p.pss_bytes for p in instance_procs if p.pss_bytes is not None)
    else:
        pss_total = None

    if not tree_complete:
        if any(v.startswith("incarnation_changed") for v in violations):
            status = "incarnation_changed"
        elif "membership_changed" in violations:
            status = "membership_changed"
        else:
            status = "partial"
    elif cg_current_bytes is None:
        status = "missing_cgroup_current"
    elif "sample_skew_exceeded" in violations:
        status = "skew_exceeded"
    elif overhead_issues:
        status = "partial_overhead"
    elif any(v.startswith("gpu_accounting_") for v in violations):
        status = f"gpu_{gpu_accounting}"
    elif cg_peak_bytes is None:
        status = "peak_unsupported"
    else:
        status = "ok"

    return MemorySample(
        sample_seq=sample_seq,
        logical_tick=logical_tick,
        monotonic_ns=end_ns,
        instance_id=instance_id,
        capsule_id=capsule_id,
        processes=tuple(proc_samples),
        cgroup_current_bytes=cg_current_bytes,
        cgroup_peak_bytes=cg_peak_bytes,
        swap_current_bytes=cg_swap_bytes,
        tree_complete=tree_complete,
        gpu_accounting=gpu_accounting,
        sample_skew_ns=skew_ns,
        status=status,
        source_policy=source_policy,
        pss_total_bytes=pss_total,
        overhead=overhead,
        cgroup_dir=cgroup_dir,
        violations=tuple(dict.fromkeys(violations)),
    )


# @anchor:wa.learn.cgroup_memory.evaluate_lifetime_peak
def evaluate_lifetime_peak(
    samples: Sequence[MemorySample],
    *,
    exclusive_ownership: bool,
    freshness_evidence: bool,
    baseline_peak_bytes: int | None = None,
    substitute_pss_for_peak: bool = False,
    python_allocation_peak_bytes: int | None = None,
    reserved_frame_bytes: int | None = None,
) -> PeakEvaluation:
    """Evaluate whole-instance RAM peak evidence under INV-J6-4.

    - Rejects peak-reset subtraction (`baseline_peak_bytes` is forbidden).
    - Rejects substituting sampled PSS peak for absent `cgroup_peak_bytes`.
    - Rejects Python allocation peaks (`tracemalloc`) or reserved frame quota bytes
      as proof of whole-instance RAM.
    - Requires both `exclusive_ownership=True` and `freshness_evidence=True` to
      admit `cgroup_peak_bytes` as authoritative lifetime peak.
    """
    if baseline_peak_bytes is not None:
        forbid_invalid_ram_arithmetic(peak_reset_subtraction=True)
    if substitute_pss_for_peak:
        forbid_invalid_ram_arithmetic(substitute_pss_for_peak=True)
    if python_allocation_peak_bytes is not None:
        raise CgroupMemoryError(
            "INV-J6-4 violation: Python allocation peak does not prove whole-instance RAM"
        )
    if reserved_frame_bytes is not None:
        raise CgroupMemoryError(
            "INV-J6-4 violation: reserved frame bytes do not prove whole-instance RAM"
        )
    if type(exclusive_ownership) is not bool or type(freshness_evidence) is not bool:
        raise CgroupMemoryError("exclusive_ownership and freshness_evidence must be bool")
    if not isinstance(samples, Sequence) or not samples:
        return PeakEvaluation(
            cgroup_peak_bytes=None,
            pss_sampled_peak_bytes=None,
            pss_peak_classification="lower_bound_diagnostic",
            lifetime_peak_admissible=False,
            exclusive_ownership=exclusive_ownership,
            freshness_evidence=freshness_evidence,
            status="AUSENTE",
            violations=("no_samples",),
        )

    pss_totals = [
        s.pss_total_bytes
        for s in samples
        if s.tree_complete and s.pss_total_bytes is not None
    ]
    pss_sampled_peak = max(pss_totals) if pss_totals else None

    cg_peaks = [s.cgroup_peak_bytes for s in samples if s.cgroup_peak_bytes is not None]
    violations: list[str] = []

    # Check monotonicity of cgroup_peak_bytes across sample_seq order if supported.
    last_peak = -1
    for s in sorted(samples, key=lambda item: item.sample_seq):
        if s.cgroup_peak_bytes is not None:
            if s.cgroup_current_bytes is not None and s.cgroup_peak_bytes < s.cgroup_current_bytes:
                violations.append(f"peak_below_current:{s.sample_seq}")
            if s.cgroup_peak_bytes < last_peak:
                violations.append(f"non_monotonic_cgroup_peak:{s.sample_seq}")
            last_peak = max(last_peak, s.cgroup_peak_bytes)

    if len(cg_peaks) != len(samples) or not cg_peaks:
        cgroup_peak = None
        violations.append("cgroup_peak_unsupported")
    else:
        cgroup_peak = max(cg_peaks)

    if not exclusive_ownership:
        violations.append("non_exclusive_cgroup_ownership")
    if not freshness_evidence:
        violations.append("missing_cgroup_freshness_evidence")

    admissible = (
        cgroup_peak is not None
        and exclusive_ownership
        and freshness_evidence
        and not violations
    )
    status = "ok" if admissible else ("peak_unsupported" if cgroup_peak is None else "AUSENTE")
    return PeakEvaluation(
        cgroup_peak_bytes=cgroup_peak,
        pss_sampled_peak_bytes=pss_sampled_peak,
        pss_peak_classification="lower_bound_diagnostic",
        lifetime_peak_admissible=admissible,
        exclusive_ownership=exclusive_ownership,
        freshness_evidence=freshness_evidence,
        status=status,
        violations=tuple(dict.fromkeys(violations)),
    )


# @anchor:wa.learn.cgroup_memory.build_holdings_pause_command
def build_holdings_pause_command(tick: int, seq: int) -> dict[str, Any]:
    """Construct an inline Holdings T2 `pause` command record (`tick`, `seq`, `cmd`, `args`)."""
    _strict_int(tick, "tick", minimum=0)
    _strict_int(seq, "seq", minimum=1)
    return {"tick": tick, "seq": seq, "cmd": "pause", "args": {}}


# @anchor:wa.learn.cgroup_memory.validate_holdings_pause_command
def validate_holdings_pause_command(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an inline Holdings T2 `pause` command record."""
    if not isinstance(record, Mapping):
        raise CgroupMemoryError("Holdings pause command must be a mapping")
    if set(record.keys()) != set(HOLDINGS_PAUSE_KEYS):
        raise CgroupMemoryError(
            f"Holdings pause command keys {sorted(record.keys())!r} != {sorted(HOLDINGS_PAUSE_KEYS)!r}"
        )
    tick = _strict_int(record["tick"], "pause.tick", minimum=0)
    seq = _strict_int(record["seq"], "pause.seq", minimum=1)
    if record["cmd"] != "pause":
        raise CgroupMemoryError(f"Holdings pause command cmd must be 'pause', got {record['cmd']!r}")
    if not isinstance(record["args"], Mapping) or dict(record["args"]) != {}:
        raise CgroupMemoryError("Holdings pause command args must be empty {}")
    return {"tick": tick, "seq": seq, "cmd": "pause", "args": {}}


# @anchor:wa.learn.cgroup_memory.validate_holdings_outcome
def validate_holdings_outcome(
    record: Mapping[str, Any],
    *,
    expected_seq: int | None = None,
    expected_tick: int | None = None,
) -> dict[str, Any]:
    """Validate an inline Holdings T2 outcome record."""
    if not isinstance(record, Mapping):
        raise CgroupMemoryError("Holdings outcome record must be a mapping")
    if set(record.keys()) != set(HOLDINGS_OUTCOME_KEYS):
        raise CgroupMemoryError(
            f"Holdings outcome keys {sorted(record.keys())!r} != {sorted(HOLDINGS_OUTCOME_KEYS)!r}"
        )
    seq = _strict_int(record["seq"], "outcome.seq", minimum=1)
    req_tick = _strict_int(record["requested_tick"], "outcome.requested_tick", minimum=0)
    app_tick = _strict_int(record["applied_tick"], "outcome.applied_tick", minimum=0)
    order_seq_id = _strict_int(record["order_seq_id"], "outcome.order_seq_id", minimum=1)
    status = record["status"]
    if not isinstance(status, str) or status not in HOLDINGS_OUTCOME_STATUSES:
        raise CgroupMemoryError(f"invalid Holdings outcome status: {status!r}")
    reason = record["reason"]
    if reason is not None and (not isinstance(reason, str) or not _REASON_RE.fullmatch(reason)):
        raise CgroupMemoryError(f"invalid Holdings outcome reason: {reason!r}")
    state_sha = record["state_sha256"]
    if not isinstance(state_sha, str) or not _HEX64_RE.fullmatch(state_sha):
        raise CgroupMemoryError("invalid Holdings outcome state_sha256")
    if expected_seq is not None and seq != _strict_int(expected_seq, "expected_seq", minimum=1):
        raise CgroupMemoryError(f"outcome seq {seq} != expected {expected_seq}")
    if expected_tick is not None and (
        req_tick != _strict_int(expected_tick, "expected_tick", minimum=0)
        or app_tick != expected_tick
    ):
        raise CgroupMemoryError(
            f"outcome ticks ({req_tick}, {app_tick}) do not match expected tick {expected_tick}"
        )
    return {
        "seq": seq,
        "requested_tick": req_tick,
        "applied_tick": app_tick,
        "order_seq_id": order_seq_id,
        "status": status,
        "reason": reason,
        "state_sha256": state_sha,
    }


def _reject_volatile_leakage(obj: Any, path: str = "$") -> None:
    if isinstance(obj, (bytes, bytearray, memoryview, np.ndarray)):
        raise CgroupMemoryError(f"volatile pixel/checkpoint bytes forbidden in persisted metadata at {path}")
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise CgroupMemoryError(f"non-string key at {path}")
            if k in _VOLATILE_FORBIDDEN_KEYS:
                raise CgroupMemoryError(f"volatile field {k!r} forbidden in persisted metadata at {path}.{k}")
            _reject_volatile_leakage(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for idx, item in enumerate(obj):
            _reject_volatile_leakage(item, f"{path}[{idx}]")


# @anchor:wa.learn.cgroup_memory.export_bounded_metadata
def export_bounded_metadata(
    record: MemorySample | GateReceipt | Mapping[str, Any],
    *,
    sink: Callable[[bytes], Any] | None = None,
) -> bytes:
    """Serialize explicitly approved bounded metadata to canonical JSON bytes in RAM.

    Refuses any raw hook receipts, UI tree dumps, opaque checkpoint bytes, or pixel
    arrays before calling `sink`.
    """
    if isinstance(record, MemorySample):
        payload: dict[str, Any] = {
            "schema": "wender.learn.cgroup_memory.sample/v1",
            "sample_seq": record.sample_seq,
            "logical_tick": record.logical_tick,
            "monotonic_ns": record.monotonic_ns,
            "instance_id": record.instance_id,
            "capsule_id": record.capsule_id,
            "processes": [
                {
                    "pid": p.pid,
                    "starttime": p.starttime,
                    "boot_id": p.boot_id,
                    "role": p.role,
                    "scope": p.scope,
                    "pss_bytes": p.pss_bytes,
                    "status": p.status,
                }
                for p in record.processes
            ],
            "cgroup_current_bytes": record.cgroup_current_bytes,
            "cgroup_peak_bytes": record.cgroup_peak_bytes,
            "swap_current_bytes": record.swap_current_bytes,
            "tree_complete": record.tree_complete,
            "gpu_accounting": record.gpu_accounting,
            "sample_skew_ns": record.sample_skew_ns,
            "status": record.status,
            "source_policy": record.source_policy,
            "pss_total_bytes": record.pss_total_bytes,
            "overhead": None if record.overhead is None else record.overhead.to_dict(),
        }
    elif isinstance(record, GateReceipt):
        payload = {
            "schema": "wender.learn.cgroup_memory.gate/v1",
            "gate_id": record.gate_id,
            "status": record.status,
            "passed": record.passed,
            "invariants": dict(record.invariants),
            "violations": list(record.violations),
            "conductor_admission_binding": record.conductor_admission_binding,
            "host_admitted": record.host_admitted,
            "retail_migration_blocked": record.retail_migration_blocked,
            "evidence": dict(record.evidence),
        }
    elif isinstance(record, Mapping):
        _reject_volatile_leakage(record)
        payload = dict(record)
    else:
        raise CgroupMemoryError("export_bounded_metadata requires MemorySample, GateReceipt, or Mapping")

    _reject_volatile_leakage(payload)
    encoded = schema.dumps(payload).encode("utf-8")
    if sink is not None:
        sink(encoded)
    return encoded


# @anchor:wa.learn.cgroup_memory.verify_earlier_wave_receipts
def verify_earlier_wave_receipts(
    receipts: Mapping[str, Any] | None,
    *,
    required_gates: Sequence[str] = ("G-M1-1", "G-M7-1", "G-M8-1"),
    required_revision: str | None = None,
) -> tuple[bool, tuple[str, ...]]:
    """Verify revision-bound earlier-wave owner gate receipts for host admission."""
    if required_revision is None or not isinstance(required_revision, str) or not required_revision.strip():
        return False, ("required_revision_AUSENTE",)
    if not isinstance(receipts, Mapping) or not receipts:
        return False, ("earlier_wave_receipts_AUSENTE",)

    missing: list[str] = []
    for gate_name in required_gates:
        rec = receipts.get(gate_name)
        if not isinstance(rec, Mapping):
            missing.append(f"missing_receipt:{gate_name}")
            continue
        if rec.get("inferred_consumer") is True or rec.get("owner_verified") is False:
            missing.append(f"inferred_consumer_label:{gate_name}")
            continue
        if rec.get("expected_failure") is True:
            missing.append(f"expected_failure_not_accepted:{gate_name}")
            continue
        if rec.get("status") != "PASS":
            missing.append(f"gate_not_pass:{gate_name}")
            continue
        if rec.get("revision") != required_revision:
            missing.append(f"revision_mismatch:{gate_name}")
            continue
        ev = rec.get("evidence")
        if not isinstance(ev, str) or not ev.strip():
            missing.append(f"empty_evidence:{gate_name}")
    return len(missing) == 0, tuple(missing)


# @anchor:wa.learn.cgroup_memory.evaluate_gate_g_j6_1
def evaluate_gate_g_j6_1(
    samples: Sequence[MemorySample],
    *,
    limits: MemoryBudgetLimits | Mapping[str, Any] | None,
    exclusive_ownership: bool = True,
    freshness_evidence: bool = True,
    earlier_wave_receipts: Mapping[str, Any] | None = None,
    required_revision: str | None = None,
    required_earlier_gates: Sequence[str] = ("G-M1-1", "G-M7-1", "G-M8-1"),
    retail_migration_disabled: bool = True,
    optional_training_failed: bool = False,
) -> GateReceipt:
    """Evaluate acceptance gate G-J6-1 and invariants INV-J6-1..INV-J6-4.

    Configured limits are required and never inferred from defaults.
    """
    if limits is None:
        raise CgroupMemoryError("configured memory limits are required; never infer defaults")
    if isinstance(limits, Mapping):
        for req_key in (
            "max_cgroup_current_bytes",
            "max_cgroup_peak_bytes",
            "max_swap_current_bytes",
            "max_sample_skew_ns",
        ):
            if req_key not in limits or limits[req_key] is None:
                raise CgroupMemoryError(f"configured limit {req_key!r} is required; never infer defaults")
        budget = MemoryBudgetLimits(
            max_cgroup_current_bytes=limits["max_cgroup_current_bytes"],
            max_cgroup_peak_bytes=limits["max_cgroup_peak_bytes"],
            max_swap_current_bytes=limits["max_swap_current_bytes"],
            max_sample_skew_ns=limits["max_sample_skew_ns"],
            max_pss_lower_bound_bytes=limits.get("max_pss_lower_bound_bytes"),
        )
    elif isinstance(limits, MemoryBudgetLimits):
        budget = limits
    else:
        raise CgroupMemoryError("limits must be MemoryBudgetLimits or Mapping")

    if not isinstance(samples, Sequence) or not samples:
        return GateReceipt(
            gate_id=GATE_ID,
            status="AUSENTE",
            passed=False,
            invariants={inv: False for inv in INVARIANTS},
            violations=("samples_AUSENTE",),
            conductor_admission_binding=CONDUCTOR_ADMISSION_BINDING,
            host_admitted=False,
            retail_migration_blocked=False,
            evidence={"sample_count": 0},
        )

    violations: list[str] = []
    inv1_ok = True
    inv2_ok = True
    inv3_ok = True
    inv4_ok = True

    last_seq = -1
    for s in samples:
        if not isinstance(s, MemorySample):
            raise CgroupMemoryError("all items in samples must be MemorySample records")
        if s.sample_seq <= last_seq:
            violations.append(f"non_increasing_sample_seq:{s.sample_seq}")
            inv2_ok = False
        last_seq = s.sample_seq

        if s.cgroup_current_bytes is None:
            inv1_ok = False
            violations.append(f"missing_cgroup_current:{s.sample_seq}")
        elif s.cgroup_current_bytes > budget.max_cgroup_current_bytes:
            violations.append(f"cgroup_current_exceeded:{s.sample_seq}")

        if s.swap_current_bytes is None:
            inv1_ok = False
            violations.append(f"missing_swap_current:{s.sample_seq}")
        elif s.swap_current_bytes > budget.max_swap_current_bytes:
            violations.append(f"swap_current_exceeded:{s.sample_seq}")

        if not s.tree_complete:
            inv2_ok = False
            inv3_ok = False
            violations.append(f"incomplete_tree:{s.sample_seq}:{s.status}")

        if s.sample_skew_ns > budget.max_sample_skew_ns:
            inv2_ok = False
            violations.append(f"sample_skew_exceeded:{s.sample_seq}")

        if s.gpu_accounting in ("unaccounted", "unsupported", "AUSENTE"):
            inv2_ok = False
            violations.append(f"gpu_accounting_incomplete:{s.sample_seq}:{s.gpu_accounting}")

        if s.overhead is None or (
            s.overhead.wender_bytes is None
            or s.overhead.capsule_bytes is None
            or s.overhead.shared_bytes is None
        ):
            inv2_ok = False
            violations.append(f"missing_overhead_separation:{s.sample_seq}")

        if budget.max_pss_lower_bound_bytes is not None and s.pss_total_bytes is not None:
            if s.pss_total_bytes > budget.max_pss_lower_bound_bytes:
                violations.append(f"pss_lower_bound_exceeded:{s.sample_seq}")

    peak_eval = evaluate_lifetime_peak(
        samples,
        exclusive_ownership=exclusive_ownership,
        freshness_evidence=freshness_evidence,
    )
    if not peak_eval.lifetime_peak_admissible or peak_eval.cgroup_peak_bytes is None:
        inv4_ok = False
        violations.extend(peak_eval.violations)
    elif peak_eval.cgroup_peak_bytes > budget.max_cgroup_peak_bytes:
        inv4_ok = False
        violations.append("cgroup_peak_exceeded")

    receipts_ok, receipt_issues = verify_earlier_wave_receipts(
        earlier_wave_receipts,
        required_gates=required_earlier_gates,
        required_revision=required_revision,
    )
    # Conductor admission binding is explicitly AUSENTE; hold host dispatch until proven.
    host_admitted = False
    if not receipts_ok:
        violations.extend(receipt_issues)

    # Optional training failures never block disabled retail Holdings migration.
    retail_migration_blocked = False if retail_migration_disabled else bool(optional_training_failed)

    invariants_map = {
        "INV-J6-1": inv1_ok,
        "INV-J6-2": inv2_ok,
        "INV-J6-3": inv3_ok,
        "INV-J6-4": inv4_ok,
    }
    gate_passed = (
        all(invariants_map.values())
        and receipts_ok
        and len(violations) == 0
    )
    status = "PASS" if gate_passed else ("AUSENTE" if not receipts_ok else "FAIL")
    return GateReceipt(
        gate_id=GATE_ID,
        status=status,
        passed=gate_passed,
        invariants=invariants_map,
        violations=tuple(dict.fromkeys(violations)),
        conductor_admission_binding=CONDUCTOR_ADMISSION_BINDING,
        host_admitted=host_admitted,
        retail_migration_blocked=retail_migration_blocked,
        evidence={
            "sample_count": len(samples),
            "cgroup_peak_bytes": peak_eval.cgroup_peak_bytes,
            "pss_sampled_peak_bytes": peak_eval.pss_sampled_peak_bytes,
            "pss_peak_classification": peak_eval.pss_peak_classification,
            "earlier_wave_receipts_verified": receipts_ok,
            "optional_training_failed": bool(optional_training_failed),
            "timing_scope": "VM",
        },
    )


__all__ = (
    "INVARIANTS",
    "GATE_ID",
    "CONDUCTOR_ADMISSION_BINDING",
    "CHROMIUM_ROLES",
    "GODOT_ROLES",
    "OVERHEAD_SCOPES",
    "GPU_ACCOUNTING_MODES",
    "SOURCE_POLICIES",
    "SAMPLE_FIELDS",
    "HOLDINGS_PAUSE_KEYS",
    "HOLDINGS_OUTCOME_KEYS",
    "CgroupMemoryError",
    "CgroupMemoryScope",
    "ProcessIdentity",
    "ProcessSample",
    "OverheadAccounting",
    "MemorySample",
    "MemoryBudgetLimits",
    "PeakEvaluation",
    "GateReceipt",
    "parse_cgroup_memory",
    "parse_smaps_rollup_pss",
    "parse_pss_kb",
    "parse_boot_id",
    "parse_proc_stat",
    "resolve_trusted_cgroup_dir",
    "enumerate_process_identities",
    "forbid_invalid_ram_arithmetic",
    "sample_instance_memory",
    "evaluate_lifetime_peak",
    "build_holdings_pause_command",
    "validate_holdings_pause_command",
    "validate_holdings_outcome",
    "export_bounded_metadata",
    "verify_earlier_wave_receipts",
    "evaluate_gate_g_j6_1",
)
