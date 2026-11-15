# @module {"id":"wa.learn.procfs_memory","layer":2,"owner":"LEARN","deps":["wa.state.schema","wa.perceive.session_registry","wa.learn.multiwin_scorecard"],"book":"HB-WA-s3-remember"}
"""Strict offline procfs/cgroup memory accounting and whole-tree identity verification (J6).

Implements INV-J6-1 through INV-J6-4 and gates G-J6-1 through G-J6-4:
- Pure strict parser `parse_smaps_rollup` converting nonnegative `kB` quantities
  at 1024 bytes per kB with overflow, duplicate, negative, malformed, and unit checks.
- Distinct strict byte parsers for cgroup `memory.current`, `memory.peak`, and
  `memory.swap.current`. Missing or unreadable inputs remain `None` (`null`) with
  explicit status (`"missing"` or `"unreadable"`), never coerced to `0`.
- Process identity binding `(pid, starttime, boot_id)` from injected `/proc/<pid>/stat`
  and `boot_id` fixtures, with pre- and post-sampling re-enumeration to detect
  membership or incarnation changes across Chromium (`parent`/`browser`, `renderer`,
  `gpu`, `utility`, `broker`, `descendant`) and native/capsule process trees while
  retaining separate `wender`, `capsule`, and `shared_overhead` buckets.
- Scope isolation forbidding PSS + cgroup addition, ancestor + child cgroup addition,
  shared browser division by tab count, and source-policy relabeling.
- Scoped peak semantics distinguishing certified lifetime cgroup peak (requiring
  exclusive ownership and freshness evidence), sampled PSS lower-bound diagnostics,
  and insufficient optional diagnostics (Python allocation peak, reserved frame bytes).
"""
from __future__ import annotations

from collections import deque
from collections.abc import Mapping
import copy
from dataclasses import dataclass, fields, is_dataclass
import json
import re
from typing import Any

import numpy as np

from wender.perceive import session_registry
from wender.state import schema

BYTES_PER_KB = 1024
MAX_MEMORY_BYTES = (1 << 63) - 1

CHROMIUM_ROLES = (
    "parent",
    "browser",
    "renderer",
    "gpu",
    "utility",
    "broker",
    "descendant",
)
OVERHEAD_SCOPES = (
    "instance",
    "wender",
    "capsule",
    "shared_overhead",
)
SOURCE_POLICIES = ("cdp", "godot")
REQUIRED_J6_GATES = ("G-J6-1", "G-J6-2", "G-J6-3", "G-J6-4")
GPU_ACCOUNTING_MODES = (
    "host_pss_only",
    "included_in_pss",
    "missing_gpu_process",
    "unreadable_gpu_process",
    "AUSENTE",
)
PEAK_CLASSIFICATIONS = (
    "certified_cgroup_lifetime_peak",
    "uncertified_cgroup_peak",
    "lower_bound_diagnostic",
    "optional_diagnostic_insufficient",
    "absent",
)

_ROLLUP_HEADER_RE = re.compile(
    r"^[0-9a-f]+-[0-9a-f]+\s+[rwxps-]{4}\s+[0-9a-f]+\s+[0-9a-f]{2}:[0-9a-f]{2}\s+\d+\s+\[rollup\]\Z"
)
_FIELD_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*\Z")
_NONNEG_INT_RE = re.compile(r"^(?:0|[1-9][0-9]*)\Z")
_SIGNED_INT_RE = re.compile(r"^(?:0|-?[1-9][0-9]*)\Z")
_BOOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_PROC_STATE_RE = re.compile(r"^[RSDZTWXxKPI]\Z")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")
_HOLDINGS_REASON_RE = re.compile(r"^[A-Z][A-Z0-9_]*\Z")

_VOLATILE_KEYS = frozenset({
    "rgb",
    "pixels",
    "frame_bytes",
    "raw_frame",
    "png",
    "jpeg",
    "jpg",
    "webp",
    "npy",
    "raw_hook_receipt",
    "hook_receipts",
    "raw_receipt",
    "raw_tree_dump",
    "tree_dump",
    "ax_raw",
    "dom_dump",
    "uitree_raw",
    "checkpoint_bytes",
    "opaque_checkpoint",
    "save_blob",
})
_DIAGNOSTIC_TIMING_KEYS = frozenset({
    "wall_time_ns",
    "wall_time_s",
    "wall_clock_timestamp",
    "host_speed",
    "measured_host_speed",
    "elapsed_wall_ns",
})


# @anchor:wa.learn.procfs_memory.ProcfsMemoryError
class ProcfsMemoryError(ValueError):
    """Strict procfs/cgroup memory accounting error with machine-readable code and status."""

    def __init__(self, message: str, *, code: str = "malformed", status: str = "malformed"):
        super().__init__(message)
        self.code = code
        self.status = status


# @anchor:wa.learn.procfs_memory.MissingProcfsError
class MissingProcfsError(ProcfsMemoryError):
    """Raised when a required procfs or cgroup input is absent."""

    def __init__(self, message: str, *, code: str = "missing"):
        super().__init__(message, code=code, status="missing")


# @anchor:wa.learn.procfs_memory.UnreadableProcfsError
class UnreadableProcfsError(ProcfsMemoryError):
    """Raised when a procfs or cgroup input is unreadable due to permissions or I/O fault."""

    def __init__(self, message: str, *, code: str = "unreadable"):
        super().__init__(message, code=code, status="unreadable")


# @anchor:wa.learn.procfs_memory.MalformedProcfsError
class MalformedProcfsError(ProcfsMemoryError):
    """Raised when a procfs or cgroup payload has bad units, negative numbers, duplicates, or overflow."""

    def __init__(self, message: str, *, code: str = "malformed"):
        super().__init__(message, code=code, status="malformed")


def _strict_int(value: Any, name: str, minimum: int = 0, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum:
        raise MalformedProcfsError(
            f"{name} must be an integer >= {minimum}",
            code="negative" if type(value) is int and value < 0 else "malformed",
        )
    if maximum is not None and value > maximum:
        raise MalformedProcfsError(
            f"{name} exceeds maximum {maximum}",
            code="overflow",
        )
    return value


def _strict_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise MalformedProcfsError(f"{name} must be a non-empty string", code="malformed")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise MalformedProcfsError(f"{name} must be valid UTF-8", code="malformed") from exc
    if any(ch in value for ch in ("\x00", "\r")):
        raise MalformedProcfsError(f"{name} contains forbidden control characters", code="malformed")
    return value


# @anchor:wa.learn.procfs_memory.SmapsRollup
@dataclass(frozen=True)
class SmapsRollup(Mapping):
    """Parsed `/proc/<pid>/smaps_rollup` quantities in bytes and kB."""

    pss_bytes: int
    pss_kb: int
    fields_bytes: dict[str, int]
    fields_kb: dict[str, int]
    header: str | None = None
    status: str = "ok"

    def __post_init__(self):
        _strict_int(self.pss_bytes, "pss_bytes", 0, MAX_MEMORY_BYTES)
        _strict_int(self.pss_kb, "pss_kb", 0, MAX_MEMORY_BYTES // BYTES_PER_KB)
        if self.pss_bytes != self.pss_kb * BYTES_PER_KB:
            raise MalformedProcfsError("pss_bytes must equal pss_kb * 1024", code="wrong_unit")

    @property
    def rss_bytes(self) -> int | None:
        return self.fields_bytes.get("Rss")

    @property
    def pss_anon_bytes(self) -> int | None:
        return self.fields_bytes.get("Pss_Anon")

    @property
    def pss_file_bytes(self) -> int | None:
        return self.fields_bytes.get("Pss_File")

    @property
    def pss_shmem_bytes(self) -> int | None:
        return self.fields_bytes.get("Pss_Shmem")

    @property
    def swap_bytes(self) -> int | None:
        return self.fields_bytes.get("Swap")

    @property
    def swap_pss_bytes(self) -> int | None:
        return self.fields_bytes.get("SwapPss")

    def _as_dict(self) -> dict[str, Any]:
        return {
            "pss_bytes": self.pss_bytes,
            "pss_kb": self.pss_kb,
            "rss_bytes": self.rss_bytes,
            "pss_anon_bytes": self.pss_anon_bytes,
            "pss_file_bytes": self.pss_file_bytes,
            "pss_shmem_bytes": self.pss_shmem_bytes,
            "swap_bytes": self.swap_bytes,
            "swap_pss_bytes": self.swap_pss_bytes,
            "fields_bytes": self.fields_bytes,
            "fields_kb": self.fields_kb,
            "header": self.header,
            "status": self.status,
        }

    def __getitem__(self, key: str) -> Any:
        d = self._as_dict()
        if key in d:
            return d[key]
        if key in self.fields_bytes:
            return self.fields_bytes[key]
        raise KeyError(key)

    def __iter__(self):
        return iter(self._as_dict())

    def __len__(self) -> int:
        return len(self._as_dict())

    def __int__(self) -> int:
        return self.pss_bytes


# @anchor:wa.learn.procfs_memory.ProcStat
@dataclass(frozen=True)
class ProcStat:
    """Parsed `/proc/<pid>/stat` process metadata and incarnation starttime."""

    pid: int
    comm: str
    state: str
    ppid: int
    starttime: int

    def __post_init__(self):
        _strict_int(self.pid, "pid", 1, MAX_MEMORY_BYTES)
        _strict_str(self.comm, "comm")
        _strict_str(self.state, "state")
        if not _PROC_STATE_RE.fullmatch(self.state):
            raise MalformedProcfsError(f"invalid proc stat state {self.state!r}", code="malformed")
        _strict_int(self.ppid, "ppid", 0, MAX_MEMORY_BYTES)
        _strict_int(self.starttime, "starttime", 0, MAX_MEMORY_BYTES)


# @anchor:wa.learn.procfs_memory.ProcessIdentity
@dataclass(frozen=True)
class ProcessIdentity:
    """Process incarnation identity `(pid, starttime, boot_id)`."""

    pid: int
    starttime: int
    boot_id: str

    def __post_init__(self):
        _strict_int(self.pid, "pid", 1, MAX_MEMORY_BYTES)
        _strict_int(self.starttime, "starttime", 0, MAX_MEMORY_BYTES)
        _strict_str(self.boot_id, "boot_id")
        if not _BOOT_ID_RE.fullmatch(self.boot_id):
            raise MalformedProcfsError(f"invalid boot_id {self.boot_id!r}", code="malformed")


# @anchor:wa.learn.procfs_memory.ProcessSample
@dataclass(frozen=True)
class ProcessSample:
    """Per-process memory and identity sample within an enumerated tree."""

    pid: int
    ppid: int | None
    role: str
    scope: str
    identity: ProcessIdentity | None
    post_identity: ProcessIdentity | None
    pss_bytes: int | None
    status: str
    comm: str | None = None

    def __post_init__(self):
        _strict_int(self.pid, "pid", 1, MAX_MEMORY_BYTES)
        if self.ppid is not None:
            _strict_int(self.ppid, "ppid", 0, MAX_MEMORY_BYTES)
        _strict_str(self.role, "role")
        _strict_str(self.scope, "scope")
        if self.scope not in OVERHEAD_SCOPES:
            raise MalformedProcfsError(f"unsupported process scope {self.scope!r}", code="malformed")
        if self.identity is not None and not isinstance(self.identity, ProcessIdentity):
            raise MalformedProcfsError("identity must be ProcessIdentity or None", code="malformed")
        if self.post_identity is not None and not isinstance(self.post_identity, ProcessIdentity):
            raise MalformedProcfsError("post_identity must be ProcessIdentity or None", code="malformed")
        if self.pss_bytes is not None:
            _strict_int(self.pss_bytes, "pss_bytes", 0, MAX_MEMORY_BYTES)
        _strict_str(self.status, "status")
        if self.status in ("missing", "unreadable") and self.pss_bytes is not None:
            raise MalformedProcfsError(
                "missing or unreadable process sample must keep pss_bytes as None",
                code="absence_violation",
            )


# @anchor:wa.learn.procfs_memory.MemorySample
@dataclass(frozen=True)
class MemorySample:
    """Whole-instance procfs/cgroup memory sample record (J6 contract field order)."""

    sample_seq: int
    logical_tick: int
    monotonic_ns: int
    instance_id: str
    capsule_id: str | None
    processes: tuple
    cgroup_current_bytes: int | None
    cgroup_peak_bytes: int | None
    swap_current_bytes: int | None
    tree_complete: bool
    gpu_accounting: str | dict
    sample_skew_ns: int | None
    status: str

    def __post_init__(self):
        _strict_int(self.sample_seq, "sample_seq", 0)
        _strict_int(self.logical_tick, "logical_tick", 0)
        _strict_int(self.monotonic_ns, "monotonic_ns", 0)
        _strict_str(self.instance_id, "instance_id")
        if self.capsule_id is not None:
            _strict_str(self.capsule_id, "capsule_id")
        if not isinstance(self.processes, tuple) or any(
            not isinstance(p, ProcessSample) for p in self.processes
        ):
            raise MalformedProcfsError(
                "processes must be a tuple of ProcessSample records",
                code="malformed",
            )
        for field_name in ("cgroup_current_bytes", "cgroup_peak_bytes", "swap_current_bytes"):
            val = getattr(self, field_name)
            if val is not None:
                _strict_int(val, field_name, 0, MAX_MEMORY_BYTES)
        if type(self.tree_complete) is not bool:
            raise MalformedProcfsError("tree_complete must be a bool", code="malformed")
        if isinstance(self.gpu_accounting, str):
            _strict_str(self.gpu_accounting, "gpu_accounting")
        elif isinstance(self.gpu_accounting, Mapping):
            _strict_str(self.gpu_accounting.get("status"), "gpu_accounting.status")
        else:
            raise MalformedProcfsError("gpu_accounting must be str or mapping", code="malformed")
        if self.sample_skew_ns is not None:
            _strict_int(self.sample_skew_ns, "sample_skew_ns", 0)
        _strict_str(self.status, "status")
        if self.tree_complete:
            if not self.processes:
                raise MalformedProcfsError(
                    "tree_complete cannot be True when processes is empty",
                    code="incomplete_tree",
                )
            for proc in self.processes:
                if (
                    proc.status != "ok"
                    or proc.pss_bytes is None
                    or proc.identity is None
                    or proc.post_identity is None
                    or proc.identity != proc.post_identity
                ):
                    raise MalformedProcfsError(
                        "tree_complete cannot be True when any member is missing, unreadable, or changed incarnation",
                        code="incomplete_tree",
                    )

    @property
    def pss_total_bytes(self) -> int | None:
        """Complete instance process-tree PSS sum; None when partial or incomplete."""
        if not self.tree_complete or self.status != "ok" or not self.processes:
            return None
        instance_procs = [p for p in self.processes if p.scope == "instance"]
        if not instance_procs or any(p.pss_bytes is None or p.status != "ok" for p in instance_procs):
            return None
        return sum(p.pss_bytes for p in instance_procs if p.pss_bytes is not None)

    @property
    def pss_lower_bound_bytes(self) -> int | None:
        """Observed instance PSS lower bound across readable members; None if none readable."""
        readable = [
            p.pss_bytes
            for p in self.processes
            if p.scope == "instance" and p.pss_bytes is not None and p.status == "ok"
        ]
        return sum(readable) if readable else None

    @property
    def role_pss_bytes(self) -> dict[str, int | None]:
        """Per-role PSS breakdown for instance processes."""
        out: dict[str, int | None] = {}
        by_role: dict[str, list[ProcessSample]] = {}
        for p in self.processes:
            if p.scope == "instance":
                by_role.setdefault(p.role, []).append(p)
        for role, members in sorted(by_role.items()):
            if all(m.pss_bytes is not None and m.status == "ok" for m in members):
                out[role] = sum(m.pss_bytes for m in members if m.pss_bytes is not None)
            else:
                out[role] = None
        return out

    def __getitem__(self, item: str) -> Any:
        if item in {f.name for f in fields(self)}:
            return getattr(self, item)
        if item in ("pss_total_bytes", "pss_lower_bound_bytes", "role_pss_bytes"):
            return getattr(self, item)
        raise KeyError(item)


ProcfsMemorySample = MemorySample


# @anchor:wa.learn.procfs_memory.OverheadScopes
@dataclass(frozen=True)
class OverheadScopes:
    """Separate PSS accounting buckets for instance, Wender, capsule, and shared overhead."""

    instance_id: str
    capsule_id: str | None
    instance_pss_bytes: int | None
    wender_pss_bytes: int | None
    capsule_pss_bytes: int | None
    shared_overhead_pss_bytes: int | None
    status: str

    def __post_init__(self):
        _strict_str(self.instance_id, "instance_id")
        if self.capsule_id is not None:
            _strict_str(self.capsule_id, "capsule_id")
        for name in (
            "instance_pss_bytes",
            "wender_pss_bytes",
            "capsule_pss_bytes",
            "shared_overhead_pss_bytes",
        ):
            val = getattr(self, name)
            if val is not None:
                _strict_int(val, name, 0, MAX_MEMORY_BYTES)
        _strict_str(self.status, "status")


# @anchor:wa.learn.procfs_memory.PeakEvaluation
@dataclass(frozen=True)
class PeakEvaluation:
    """Scoped peak evaluation record enforcing Gate G-J6-4 semantics."""

    instance_id: str
    peak_kind: str
    classification: str
    reported_bytes: int | None
    configured_limit_bytes: int | None
    sufficient_for_whole_instance_ram: bool
    within_limit: bool | None
    exclusive_ownership: bool
    freshness_evidence: str | None
    pss_sampled_peak_bytes: int | None
    cgroup_peak_bytes: int | None
    diagnostics: dict[str, Any]
    violations: tuple[str, ...]
    status: str


# @anchor:wa.learn.procfs_memory.MemoryBudgetConfig
@dataclass(frozen=True)
class MemoryBudgetConfig:
    """Explicit authored memory budget configuration; limits are never defaulted."""

    max_instance_ram_bytes: int
    max_cgroup_peak_bytes: int
    max_swap_bytes: int
    max_sample_skew_ns: int
    max_parse_bytes: int = MAX_MEMORY_BYTES

    def __post_init__(self):
        for name in (
            "max_instance_ram_bytes",
            "max_cgroup_peak_bytes",
            "max_swap_bytes",
            "max_sample_skew_ns",
            "max_parse_bytes",
        ):
            val = getattr(self, name)
            if val is None:
                raise MissingProcfsError(
                    f"configured limit {name} is AUSENTE; inferred defaults are forbidden",
                    code="limit_ausente",
                )
            _strict_int(val, name, 0 if name == "max_swap_bytes" else 1, MAX_MEMORY_BYTES)


# @anchor:wa.learn.procfs_memory.MemoryGateReport
@dataclass(frozen=True)
class MemoryGateReport:
    """Complete J6 gate evaluation report."""

    instance_id: str
    source_policy: str
    samples: tuple[MemorySample, ...]
    overheads: OverheadScopes
    peak_evaluation: PeakEvaluation
    gates: dict[str, dict[str, Any]]
    violations: tuple[str, ...]
    absent: dict[str, str]
    passed: bool


def _classify_source_error(raw: Any, label: str) -> None:
    if raw is None:
        raise MissingProcfsError(f"{label} is missing (None)", code="missing")
    if isinstance(raw, type) and issubclass(raw, BaseException):
        if issubclass(raw, FileNotFoundError):
            raise MissingProcfsError(f"{label} is missing (FileNotFoundError)", code="missing")
        if issubclass(raw, (PermissionError, OSError)):
            raise UnreadableProcfsError(f"{label} is unreadable ({raw.__name__})", code="unreadable")
        raise MalformedProcfsError(f"{label} raised {raw.__name__}", code="malformed")
    if isinstance(raw, BaseException):
        if isinstance(raw, FileNotFoundError):
            raise MissingProcfsError(f"{label} is missing ({raw})", code="missing")
        if isinstance(raw, (PermissionError, OSError)):
            raise UnreadableProcfsError(f"{label} is unreadable ({raw})", code="unreadable")
        raise MalformedProcfsError(f"{label} raised {type(raw).__name__}", code="malformed")
    if isinstance(raw, Mapping):
        st = raw.get("status")
        if st == "missing":
            raise MissingProcfsError(f"{label} marked missing", code="missing")
        if st == "unreadable":
            raise UnreadableProcfsError(f"{label} marked unreadable", code="unreadable")


# @anchor:wa.learn.procfs_memory.parse_smaps_rollup
def parse_smaps_rollup(
    raw_text: Any,
    *,
    max_bytes: int = MAX_MEMORY_BYTES,
) -> SmapsRollup:
    """Pure strict parser for `/proc/<pid>/smaps_rollup` text fixtures.

    - Requires unit `kB` on every metric line and converts by `1024` bytes per `kB`.
    - Rejects duplicate keys, negative values, malformed numbers, non-ASCII digits,
      wrong units (`B`, `MB`, `KiB`, `kb`, or unitless), and values whose byte
      conversion exceeds `max_bytes`.
    - Distinguishes missing (`MissingProcfsError`) and unreadable (`UnreadableProcfsError`)
      inputs, and rejects rollup payloads that omit `Pss` (`code="missing_pss"`).
    """
    _strict_int(max_bytes, "max_bytes", 1, MAX_MEMORY_BYTES)
    _classify_source_error(raw_text, "smaps_rollup")
    if not isinstance(raw_text, str):
        raise MalformedProcfsError("smaps_rollup payload must be a string", code="malformed")
    try:
        raw_text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise MalformedProcfsError("smaps_rollup contains invalid UTF-8", code="malformed") from exc
    if "\r" in raw_text or "\x00" in raw_text:
        raise MalformedProcfsError("smaps_rollup contains forbidden control characters", code="malformed")
    if not raw_text or not raw_text.strip():
        raise MissingProcfsError("smaps_rollup payload is empty", code="missing_pss")

    lines = raw_text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        raise MissingProcfsError("smaps_rollup payload has no lines", code="missing_pss")

    header: str | None = None
    fields_kb: dict[str, int] = {}
    fields_bytes: dict[str, int] = {}

    for idx, line in enumerate(lines):
        if not line:
            raise MalformedProcfsError("blank line inside smaps_rollup", code="malformed")
        if idx == 0 and _ROLLUP_HEADER_RE.fullmatch(line):
            header = line
            continue
        if ":" not in line:
            raise MalformedProcfsError(f"malformed smaps_rollup line: {line!r}", code="malformed")
        key, _, rhs = line.partition(":")
        if not _FIELD_KEY_RE.fullmatch(key):
            raise MalformedProcfsError(f"invalid smaps_rollup field name: {key!r}", code="malformed")
        if key in fields_kb:
            raise MalformedProcfsError(f"duplicate smaps_rollup field: {key}", code="duplicate")
        if any(ch in rhs for ch in ("\t", "\v", "\f")):
            raise MalformedProcfsError(f"forbidden whitespace in smaps_rollup field {key}", code="malformed")
        tokens = [tok for tok in rhs.strip(" ").split(" ") if tok != ""]
        if not tokens:
            raise MalformedProcfsError(f"missing value for smaps_rollup field {key}", code="malformed")
        if len(tokens) == 1:
            tok = tokens[0]
            if tok.startswith("-"):
                raise MalformedProcfsError(f"negative value for {key}: {tok}", code="negative")
            if _NONNEG_INT_RE.fullmatch(tok):
                raise MalformedProcfsError(f"missing unit for {key}: expected 'kB'", code="wrong_unit")
            raise MalformedProcfsError(f"malformed value for {key}: {tok!r}", code="malformed")
        if len(tokens) != 2:
            raise MalformedProcfsError(f"malformed tokens for {key}: {tokens!r}", code="malformed")

        num_str, unit = tokens
        if num_str.startswith("-"):
            raise MalformedProcfsError(f"negative value for {key}: {num_str}", code="negative")
        if not _NONNEG_INT_RE.fullmatch(num_str):
            raise MalformedProcfsError(f"malformed integer for {key}: {num_str!r}", code="malformed")
        if unit != "kB":
            raise MalformedProcfsError(
                f"wrong unit for {key}: expected 'kB', got {unit!r}",
                code="wrong_unit",
            )
        kb_value = int(num_str)
        byte_value = kb_value * BYTES_PER_KB
        if byte_value > max_bytes or byte_value > MAX_MEMORY_BYTES:
            raise MalformedProcfsError(
                f"overflow converting {key}={kb_value} kB to bytes (limit {max_bytes})",
                code="overflow",
            )
        fields_kb[key] = kb_value
        fields_bytes[key] = byte_value

    if "Pss" not in fields_bytes:
        raise MissingProcfsError("smaps_rollup is missing required Pss field", code="missing_pss")

    return SmapsRollup(
        pss_bytes=fields_bytes["Pss"],
        pss_kb=fields_kb["Pss"],
        fields_bytes=fields_bytes,
        fields_kb=fields_kb,
        header=header,
        status="ok",
    )


# @anchor:wa.learn.procfs_memory.probe_smaps_rollup
def probe_smaps_rollup(
    raw_source: Any,
    *,
    max_bytes: int = MAX_MEMORY_BYTES,
) -> dict[str, Any]:
    """Probe an injected `smaps_rollup` fixture, keeping missing/unreadable as `None`."""
    try:
        if callable(raw_source):
            raw_source = raw_source()
        rollup = parse_smaps_rollup(raw_source, max_bytes=max_bytes)
        return {
            "status": "ok",
            "pss_bytes": rollup.pss_bytes,
            "pss_kb": rollup.pss_kb,
            "rollup": rollup,
            "error_code": None,
        }
    except MissingProcfsError as exc:
        if exc.code == "missing_pss":
            raise
        return {
            "status": "missing",
            "pss_bytes": None,
            "pss_kb": None,
            "rollup": None,
            "error_code": exc.code,
        }
    except UnreadableProcfsError as exc:
        return {
            "status": "unreadable",
            "pss_bytes": None,
            "pss_kb": None,
            "rollup": None,
            "error_code": exc.code,
        }


# @anchor:wa.learn.procfs_memory.parse_cgroup_bytes
def parse_cgroup_bytes(
    raw_text: Any,
    *,
    field_name: str = "memory.current",
    max_bytes: int = MAX_MEMORY_BYTES,
) -> int:
    """Strict parser for cgroup v2 byte files (`memory.current`, `memory.peak`, `memory.swap.current`)."""
    _strict_int(max_bytes, "max_bytes", 1, MAX_MEMORY_BYTES)
    _strict_str(field_name, "field_name")
    _classify_source_error(raw_text, field_name)
    if not isinstance(raw_text, str):
        raise MalformedProcfsError(f"{field_name} payload must be a string", code="malformed")
    try:
        raw_text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise MalformedProcfsError(f"{field_name} contains invalid UTF-8", code="malformed") from exc
    if "\r" in raw_text or "\x00" in raw_text:
        raise MalformedProcfsError(f"{field_name} contains forbidden control characters", code="malformed")
    text = raw_text[:-1] if raw_text.endswith("\n") else raw_text
    if not text:
        raise MalformedProcfsError(f"{field_name} payload is empty", code="malformed")
    if "\n" in text:
        raise MalformedProcfsError(f"duplicate or multiline value in {field_name}", code="duplicate")
    if text != text.strip():
        raise MalformedProcfsError(f"leading or trailing whitespace in {field_name}", code="malformed")
    tokens = text.split(" ")
    if len(tokens) > 1:
        if tokens[0].startswith("-"):
            raise MalformedProcfsError(f"negative value in {field_name}: {text}", code="negative")
        raise MalformedProcfsError(
            f"unexpected unit or extra token in byte quantity {field_name}: {text!r}",
            code="wrong_unit",
        )
    if text.startswith("-"):
        raise MalformedProcfsError(f"negative byte quantity in {field_name}: {text}", code="negative")
    if not _NONNEG_INT_RE.fullmatch(text):
        raise MalformedProcfsError(f"malformed byte quantity in {field_name}: {text!r}", code="malformed")
    value = int(text)
    if value > max_bytes or value > MAX_MEMORY_BYTES:
        raise MalformedProcfsError(
            f"{field_name}={value} exceeds max_bytes={max_bytes}",
            code="overflow",
        )
    return value


# @anchor:wa.learn.procfs_memory.probe_cgroup_memory
def probe_cgroup_memory(
    cgroup_mapping: Mapping[str, Any] | None,
    *,
    max_bytes: int = MAX_MEMORY_BYTES,
) -> dict[str, Any]:
    """Parse distinct cgroup v2 `memory.current`, `memory.peak`, and `memory.swap.current` fixtures.

    Missing or unreadable files stay `None` (`null`) with per-file and aggregate status, never `0`.
    """
    file_keys = (
        ("memory.current", "cgroup_current_bytes"),
        ("memory.peak", "cgroup_peak_bytes"),
        ("memory.swap.current", "swap_current_bytes"),
    )
    if cgroup_mapping is None:
        return {
            "cgroup_current_bytes": None,
            "cgroup_peak_bytes": None,
            "swap_current_bytes": None,
            "file_status": {k: "missing" for k, _ in file_keys},
            "status": "missing",
        }
    if not isinstance(cgroup_mapping, Mapping):
        raise MalformedProcfsError("cgroup_mapping must be a Mapping or None", code="malformed")

    values: dict[str, int | None] = {}
    statuses: dict[str, str] = {}
    for file_name, attr_name in file_keys:
        if file_name not in cgroup_mapping:
            values[attr_name] = None
            statuses[file_name] = "missing"
            continue
        raw_entry = cgroup_mapping[file_name]
        try:
            if callable(raw_entry):
                raw_entry = raw_entry()
            values[attr_name] = parse_cgroup_bytes(
                raw_entry,
                field_name=file_name,
                max_bytes=max_bytes,
            )
            statuses[file_name] = "ok"
        except MissingProcfsError:
            values[attr_name] = None
            statuses[file_name] = "missing"
        except UnreadableProcfsError:
            values[attr_name] = None
            statuses[file_name] = "unreadable"

    status_set = set(statuses.values())
    if status_set == {"ok"}:
        overall = "ok"
    elif status_set == {"missing"}:
        overall = "missing"
    elif "unreadable" in status_set and "ok" not in status_set:
        overall = "unreadable"
    else:
        overall = "partial"

    return {
        "cgroup_current_bytes": values["cgroup_current_bytes"],
        "cgroup_peak_bytes": values["cgroup_peak_bytes"],
        "swap_current_bytes": values["swap_current_bytes"],
        "file_status": statuses,
        "status": overall,
    }


# @anchor:wa.learn.procfs_memory.parse_boot_id
def parse_boot_id(raw_text: Any) -> str:
    """Strict parser for `/proc/sys/kernel/random/boot_id` text fixtures."""
    _classify_source_error(raw_text, "boot_id")
    if not isinstance(raw_text, str):
        raise MalformedProcfsError("boot_id must be a string", code="malformed")
    try:
        raw_text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise MalformedProcfsError("boot_id contains invalid UTF-8", code="malformed") from exc
    if "\r" in raw_text or "\x00" in raw_text:
        raise MalformedProcfsError("boot_id contains forbidden control characters", code="malformed")
    text = raw_text[:-1] if raw_text.endswith("\n") else raw_text
    if not text or "\n" in text or text != text.strip():
        raise MalformedProcfsError(f"malformed boot_id: {raw_text!r}", code="malformed")
    if not _BOOT_ID_RE.fullmatch(text):
        raise MalformedProcfsError(f"invalid boot_id token: {text!r}", code="malformed")
    return text


# @anchor:wa.learn.procfs_memory.parse_proc_stat
def parse_proc_stat(
    raw_text: Any,
    *,
    expected_pid: int | None = None,
) -> ProcStat:
    """Strict parser for `/proc/<pid>/stat` text fixtures.

    Extracts `(pid, comm, state, ppid, starttime)` where `starttime` is field 22
    (index 19 after the closing `)` of `comm`), validating integer fields strictly.
    """
    if expected_pid is not None:
        _strict_int(expected_pid, "expected_pid", 1, MAX_MEMORY_BYTES)
    _classify_source_error(raw_text, "proc_stat")
    if not isinstance(raw_text, str):
        raise MalformedProcfsError("proc_stat payload must be a string", code="malformed")
    try:
        raw_text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise MalformedProcfsError("proc_stat contains invalid UTF-8", code="malformed") from exc
    if "\r" in raw_text or "\x00" in raw_text:
        raise MalformedProcfsError("proc_stat contains forbidden control characters", code="malformed")
    line = raw_text[:-1] if raw_text.endswith("\n") else raw_text
    if not line or "\n" in line:
        raise MalformedProcfsError("proc_stat must be a single non-empty line", code="malformed")

    lpar = line.find("(")
    rpar = line.rfind(")")
    if lpar <= 0 or rpar <= lpar + 1 or line[lpar - 1] != " ":
        raise MalformedProcfsError(f"malformed comm delimiters in proc_stat: {line!r}", code="malformed")

    pid_str = line[: lpar - 1]
    if pid_str.startswith("-"):
        raise MalformedProcfsError(f"negative pid in proc_stat: {pid_str}", code="negative")
    if not _NONNEG_INT_RE.fullmatch(pid_str):
        raise MalformedProcfsError(f"malformed pid in proc_stat: {pid_str!r}", code="malformed")
    pid = int(pid_str)
    if pid < 1 or pid > MAX_MEMORY_BYTES:
        raise MalformedProcfsError(f"out-of-range pid in proc_stat: {pid}", code="malformed")
    if expected_pid is not None and pid != expected_pid:
        raise MalformedProcfsError(
            f"proc_stat pid {pid} does not match expected_pid {expected_pid}",
            code="pid_mismatch",
        )

    comm = line[lpar + 1 : rpar]
    if rpar + 2 >= len(line) or line[rpar + 1] != " ":
        raise MalformedProcfsError("missing fields after comm in proc_stat", code="malformed")

    after = line[rpar + 2 :].split(" ")
    if len(after) < 20 or any(tok == "" for tok in after):
        raise MalformedProcfsError(
            f"proc_stat requires at least 22 fields (got {len(after) + 2})",
            code="malformed",
        )

    state = after[0]
    if not _PROC_STATE_RE.fullmatch(state):
        raise MalformedProcfsError(f"invalid state field in proc_stat: {state!r}", code="malformed")

    ppid_str = after[1]
    if ppid_str.startswith("-"):
        raise MalformedProcfsError(f"negative ppid in proc_stat: {ppid_str}", code="negative")
    if not _NONNEG_INT_RE.fullmatch(ppid_str):
        raise MalformedProcfsError(f"malformed ppid in proc_stat: {ppid_str!r}", code="malformed")
    ppid = int(ppid_str)

    starttime_str = after[19]
    if starttime_str.startswith("-"):
        raise MalformedProcfsError(
            f"negative starttime in proc_stat: {starttime_str}",
            code="negative",
        )
    if not _NONNEG_INT_RE.fullmatch(starttime_str):
        raise MalformedProcfsError(
            f"malformed starttime in proc_stat: {starttime_str!r}",
            code="malformed",
        )
    starttime = int(starttime_str)
    if starttime > MAX_MEMORY_BYTES:
        raise MalformedProcfsError("starttime overflow in proc_stat", code="overflow")

    for idx, tok in enumerate(after[2:], start=5):
        if idx == 22:
            continue
        if not _SIGNED_INT_RE.fullmatch(tok):
            raise MalformedProcfsError(
                f"malformed numeric field {idx} in proc_stat: {tok!r}",
                code="malformed",
            )

    return ProcStat(pid=pid, comm=comm, state=state, ppid=ppid, starttime=starttime)


parse_stat = parse_proc_stat
parse_procfs_stat = parse_proc_stat


# @anchor:wa.learn.procfs_memory.parse_process_identity
def parse_process_identity(
    stat_or_pid: Any,
    boot_or_stat: Any = None,
    boot_id_arg: Any = None,
    *,
    expected_pid: int | None = None,
    pid: int | None = None,
    boot_id: Any = None,
) -> ProcessIdentity:
    """Bind process identity `(pid, starttime, boot_id)` from injected stat and boot fixtures."""
    if type(stat_or_pid) is int:
        eff_pid = stat_or_pid
        stat_text = boot_or_stat
        boot_id_text = boot_id_arg if boot_id_arg is not None else boot_id
    else:
        eff_pid = expected_pid if expected_pid is not None else pid
        stat_text = stat_or_pid
        boot_id_text = boot_or_stat if boot_or_stat is not None else boot_id
    stat = parse_proc_stat(stat_text, expected_pid=eff_pid)
    parsed_boot_id = parse_boot_id(boot_id_text)
    return ProcessIdentity(pid=stat.pid, starttime=stat.starttime, boot_id=parsed_boot_id)


# @anchor:wa.learn.procfs_memory.classify_process_role
def classify_process_role(comm_or_role: str, *, is_root: bool = False) -> str:
    """Classify a process into Chromium or native tree roles (`parent`, `renderer`, `gpu`, `utility`, `broker`, `descendant`)."""
    token = _strict_str(comm_or_role, "comm_or_role").lower()
    if token in CHROMIUM_ROLES:
        return "parent" if token == "browser" and is_root else token
    if "renderer" in token:
        return "renderer"
    if "gpu" in token:
        return "gpu"
    if "utility" in token or "network" in token or "audio" in token or "storage" in token:
        return "utility"
    if "broker" in token or "zygote" in token or "sandbox" in token:
        return "broker"
    if is_root or token in ("chrome", "chromium", "godot", "holdings"):
        return "parent"
    return "descendant"


def _probe_stat_and_identity(
    pid: int,
    stat_source: Any,
    boot_id_source: Any,
) -> tuple[ProcStat | None, ProcessIdentity | None, str]:
    try:
        raw_stat = stat_source() if callable(stat_source) else stat_source
        stat = parse_proc_stat(raw_stat, expected_pid=pid)
    except MissingProcfsError:
        return None, None, "missing"
    except UnreadableProcfsError:
        return None, None, "unreadable"

    try:
        raw_boot = boot_id_source() if callable(boot_id_source) else boot_id_source
        boot_id = parse_boot_id(raw_boot)
    except MissingProcfsError:
        return stat, None, "missing"
    except UnreadableProcfsError:
        return stat, None, "unreadable"

    return stat, ProcessIdentity(pid=stat.pid, starttime=stat.starttime, boot_id=boot_id), "ok"


def _discover_descendants(
    root_pids: tuple[int, ...],
    stats_by_pid: Mapping[int, ProcStat | None],
    explicit_pids: set[int],
) -> list[int]:
    children_map: dict[int, list[int]] = {}
    for pid, st in sorted(stats_by_pid.items()):
        if st is not None:
            children_map.setdefault(st.ppid, []).append(pid)
    visited: set[int] = set()
    queue = deque(sorted(set(root_pids) | explicit_pids))
    ordered: list[int] = []
    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        ordered.append(current)
        for child in sorted(children_map.get(current, ())):
            if child not in visited:
                queue.append(child)
    return sorted(ordered)


# @anchor:wa.learn.procfs_memory.sample_process_tree
def sample_process_tree(
    *,
    sample_seq: int,
    logical_tick: int,
    clock: Any,
    instance_id: str,
    capsule_id: str | None = None,
    root_pids: tuple[int, ...] | list[int],
    pre_stat_map: Mapping[int, Any],
    post_stat_map: Mapping[int, Any] | None = None,
    smaps_map: Mapping[int, Any],
    pre_boot_id: Any,
    post_boot_id: Any = None,
    cgroup_files: Mapping[str, Any] | None = None,
    role_map: Mapping[int, str] | None = None,
    scope_map: Mapping[int, str] | None = None,
    expected_member_pids: tuple[int, ...] | list[int] | set[int] | None = None,
    expect_gpu_process: bool = False,
    gpu_mode: str | None = None,
    sample_delay_ns: int = 0,
    max_bytes: int = MAX_MEMORY_BYTES,
    max_sample_skew_ns: int | None = None,
) -> tuple[MemorySample, OverheadScopes]:
    """Sample a whole process tree and optional cgroup scope with pre/post re-enumeration.

    - Re-enumerates process membership and `(pid, starttime, boot_id)` before and
      after reading `smaps_rollup` to detect process exits, spawns, or PID reuse.
    - Retains separate `instance`, `wender`, `capsule`, and `shared_overhead` PSS buckets.
    - Never sums PSS with cgroup bytes; missing or unreadable members stay `None`
      and mark `tree_complete = False` and `status = "partial"` (or specific change status).
    """
    _strict_int(sample_seq, "sample_seq", 0)
    _strict_int(logical_tick, "logical_tick", 0)
    _strict_str(instance_id, "instance_id")
    if capsule_id is not None:
        _strict_str(capsule_id, "capsule_id")
    if not callable(clock):
        raise MalformedProcfsError("clock must be an injected callable", code="malformed")
    if not isinstance(root_pids, (tuple, list)) or not root_pids:
        raise MalformedProcfsError("root_pids must be a non-empty tuple or list", code="malformed")
    roots = tuple(_strict_int(p, "root_pid", 1, MAX_MEMORY_BYTES) for p in root_pids)
    if not isinstance(pre_stat_map, Mapping) or not isinstance(smaps_map, Mapping):
        raise MalformedProcfsError("pre_stat_map and smaps_map must be mappings", code="malformed")
    if post_stat_map is None:
        post_stat_map = pre_stat_map
    elif not isinstance(post_stat_map, Mapping):
        raise MalformedProcfsError("post_stat_map must be a mapping", code="malformed")
    if post_boot_id is None:
        post_boot_id = pre_boot_id
    role_map = dict(role_map or {})
    scope_map = dict(scope_map or {})
    _strict_int(sample_delay_ns, "sample_delay_ns", 0)
    if max_sample_skew_ns is not None:
        _strict_int(max_sample_skew_ns, "max_sample_skew_ns", 0)

    pre_ns = _strict_int(clock(), "pre_monotonic_ns", 0)

    # Pre-sample enumeration
    pre_stats: dict[int, ProcStat | None] = {}
    pre_identities: dict[int, ProcessIdentity | None] = {}
    pre_statuses: dict[int, str] = {}
    for raw_pid, raw_stat in pre_stat_map.items():
        pid = _strict_int(raw_pid, "pid", 1, MAX_MEMORY_BYTES)
        st, ident, status = _probe_stat_and_identity(pid, raw_stat, pre_boot_id)
        pre_stats[pid] = st
        pre_identities[pid] = ident
        pre_statuses[pid] = status

    explicit_expected = {
        _strict_int(p, "expected_pid", 1, MAX_MEMORY_BYTES)
        for p in (expected_member_pids or ())
    }
    pre_tree_pids = _discover_descendants(roots, pre_stats, explicit_expected | set(pre_stat_map.keys()))

    # Sample smaps_rollup for all candidate PIDs
    smaps_results: dict[int, dict[str, Any]] = {}
    all_candidate_pids = sorted(
        set(pre_tree_pids) | set(post_stat_map.keys()) | set(smaps_map.keys()) | explicit_expected
    )
    for pid in all_candidate_pids:
        if pid in smaps_map:
            smaps_results[pid] = probe_smaps_rollup(smaps_map[pid], max_bytes=max_bytes)
        else:
            smaps_results[pid] = {
                "status": "missing",
                "pss_bytes": None,
                "pss_kb": None,
                "rollup": None,
                "error_code": "missing",
            }

    if sample_delay_ns > 0 and callable(getattr(clock, "advance", None)):
        clock.advance(sample_delay_ns)

    # Post-sample re-enumeration
    post_stats: dict[int, ProcStat | None] = {}
    post_identities: dict[int, ProcessIdentity | None] = {}
    post_statuses: dict[int, str] = {}
    for raw_pid, raw_stat in post_stat_map.items():
        pid = _strict_int(raw_pid, "pid", 1, MAX_MEMORY_BYTES)
        st, ident, status = _probe_stat_and_identity(pid, raw_stat, post_boot_id)
        post_stats[pid] = st
        post_identities[pid] = ident
        post_statuses[pid] = status

    post_ns = _strict_int(clock(), "post_monotonic_ns", 0)
    if post_ns < pre_ns:
        raise MalformedProcfsError("monotonic clock moved backwards during sample", code="clock_backwards")
    sample_skew_ns = post_ns - pre_ns

    post_tree_pids = _discover_descendants(
        roots,
        post_stats,
        explicit_expected | set(post_stat_map.keys()),
    )

    pre_alive_pids = {pid for pid, st in pre_statuses.items() if st == "ok"}
    post_alive_pids = {pid for pid, st in post_statuses.items() if st == "ok"}
    membership_changed = pre_alive_pids != post_alive_pids or set(pre_tree_pids) != set(post_tree_pids)
    incarnation_changed = False

    process_samples: list[ProcessSample] = []
    for pid in all_candidate_pids:
        pre_st = pre_stats.get(pid)
        post_st = post_stats.get(pid)
        pre_id = pre_identities.get(pid)
        post_id = post_identities.get(pid)
        pre_stat_status = pre_statuses.get(pid, "missing")
        post_stat_status = post_statuses.get(pid, "missing")
        smaps_info = smaps_results[pid]
        smaps_status = smaps_info["status"]

        comm = (
            pre_st.comm
            if pre_st is not None
            else (post_st.comm if post_st is not None else None)
        )
        ppid = (
            pre_st.ppid
            if pre_st is not None
            else (post_st.ppid if post_st is not None else None)
        )
        raw_role = role_map.get(pid) or (comm or "descendant")
        role = classify_process_role(raw_role, is_root=(pid in roots))
        scope = scope_map.get(pid, "instance")

        if pre_id is not None and post_id is not None and pre_id != post_id:
            incarnation_changed = True
            proc_status = "incarnation_changed"
            pss_val = None
        elif pre_st is not None and post_st is not None and pre_st.ppid != post_st.ppid:
            membership_changed = True
            proc_status = "membership_changed"
            pss_val = None
        elif (pid in pre_alive_pids) != (pid in post_alive_pids):
            membership_changed = True
            proc_status = "membership_changed"
            pss_val = None
        elif "unreadable" in (pre_stat_status, post_stat_status, smaps_status):
            proc_status = "unreadable"
            pss_val = None
        elif "missing" in (pre_stat_status, post_stat_status, smaps_status):
            proc_status = "missing"
            pss_val = None
        else:
            proc_status = "ok"
            pss_val = smaps_info["pss_bytes"]

        process_samples.append(
            ProcessSample(
                pid=pid,
                ppid=ppid,
                role=role,
                scope=scope,
                identity=pre_id,
                post_identity=post_id,
                pss_bytes=pss_val,
                status=proc_status,
                comm=comm,
            )
        )

    cgroup_info = probe_cgroup_memory(cgroup_files, max_bytes=max_bytes)

    instance_procs = [p for p in process_samples if p.scope == "instance"]
    gpu_procs = [p for p in instance_procs if p.role == "gpu"]
    if gpu_mode is not None:
        _strict_str(gpu_mode, "gpu_mode")
        gpu_accounting: str | dict = gpu_mode
    elif expect_gpu_process and not gpu_procs:
        gpu_accounting = "missing_gpu_process"
    elif gpu_procs:
        if any(p.status == "unreadable" for p in gpu_procs):
            gpu_accounting = "unreadable_gpu_process"
        elif any(p.status != "ok" or p.pss_bytes is None for p in gpu_procs):
            gpu_accounting = "missing_gpu_process"
        else:
            gpu_accounting = "host_pss_only"
    else:
        gpu_accounting = "AUSENTE"

    skew_exceeded = (
        max_sample_skew_ns is not None and sample_skew_ns > max_sample_skew_ns
    )
    all_procs_ok = bool(instance_procs) and all(
        p.status == "ok"
        and p.pss_bytes is not None
        and p.identity is not None
        and p.identity == p.post_identity
        for p in process_samples
    )
    gpu_ok = gpu_accounting not in ("missing_gpu_process", "unreadable_gpu_process")
    cgroup_ok = cgroup_info["status"] in ("ok", "missing") if cgroup_files is None else cgroup_info["status"] == "ok"

    tree_complete = (
        all_procs_ok
        and not membership_changed
        and not incarnation_changed
        and not skew_exceeded
        and gpu_ok
    )

    if incarnation_changed:
        sample_status = "incarnation_changed"
    elif membership_changed:
        sample_status = "membership_changed"
    elif skew_exceeded:
        sample_status = "skew_exceeded"
    elif not tree_complete or not cgroup_ok:
        sample_status = "partial"
    else:
        sample_status = "ok"

    scope_totals: dict[str, int | None] = {}
    for sc in OVERHEAD_SCOPES:
        sc_procs = [p for p in process_samples if p.scope == sc]
        if not sc_procs:
            scope_totals[sc] = None
        elif all(p.status == "ok" and p.pss_bytes is not None for p in sc_procs) and (
            sc != "instance" or tree_complete
        ):
            scope_totals[sc] = sum(p.pss_bytes for p in sc_procs if p.pss_bytes is not None)
        else:
            scope_totals[sc] = None

    overheads = OverheadScopes(
        instance_id=instance_id,
        capsule_id=capsule_id,
        instance_pss_bytes=scope_totals["instance"],
        wender_pss_bytes=scope_totals["wender"],
        capsule_pss_bytes=scope_totals["capsule"],
        shared_overhead_pss_bytes=scope_totals["shared_overhead"],
        status="ok" if sample_status == "ok" else "partial",
    )

    sample = MemorySample(
        sample_seq=sample_seq,
        logical_tick=logical_tick,
        monotonic_ns=pre_ns,
        instance_id=instance_id,
        capsule_id=capsule_id,
        processes=tuple(process_samples),
        cgroup_current_bytes=cgroup_info["cgroup_current_bytes"],
        cgroup_peak_bytes=cgroup_info["cgroup_peak_bytes"],
        swap_current_bytes=cgroup_info["swap_current_bytes"],
        tree_complete=tree_complete,
        gpu_accounting=gpu_accounting,
        sample_skew_ns=sample_skew_ns,
        status=sample_status,
    )
    return sample, overheads


def _normalize_cgroup_path(path: str) -> str:
    text = _strict_str(path, "cgroup_path").strip()
    if not text.startswith("/"):
        text = "/" + text
    parts = [p for p in text.split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise MalformedProcfsError(f"invalid relative segment in cgroup_path {path!r}", code="malformed")
    return "/" + "/".join(parts)


# @anchor:wa.learn.procfs_memory.validate_scope_isolation
def validate_scope_isolation(
    *,
    add_pss_and_cgroup: bool = False,
    cgroup_paths: tuple[str, ...] | list[str] = (),
    tab_count: int = 1,
    divide_shared_browser_by_tabs: bool = False,
    source_policy: str | None = None,
    handle_kind: str | None = None,
    merge_overhead_into_instance: bool = False,
) -> tuple[str, ...]:
    """Check INV-J6-3 scope isolation rules and return any violation codes."""
    violations: list[str] = []
    if add_pss_and_cgroup:
        violations.append("pss_cgroup_addition_forbidden")
    if divide_shared_browser_by_tabs or (_strict_int(tab_count, "tab_count", 1) > 1 and divide_shared_browser_by_tabs):
        violations.append("shared_browser_tab_division_forbidden")
    if merge_overhead_into_instance:
        violations.append("overhead_scope_conflation_forbidden")

    normalized = [_normalize_cgroup_path(p) for p in cgroup_paths]
    for i, first in enumerate(normalized):
        for j, second in enumerate(normalized):
            if i >= j:
                continue
            if first == second or first == "/" or second == "/" or first.startswith(second + "/") or second.startswith(first + "/"):
                violations.append("ancestor_child_scope_addition_forbidden")
                break

    if source_policy is not None:
        if source_policy not in SOURCE_POLICIES:
            violations.append("unsupported_source_policy")
        elif handle_kind is not None:
            if handle_kind not in session_registry.SUPPORTED_KINDS or handle_kind not in schema.TARGET_KINDS:
                violations.append("unsupported_handle_kind")
            elif source_policy == "cdp" and handle_kind == "godot_window":
                violations.append("source_policy_relabeling_forbidden")
            elif source_policy == "godot" and handle_kind == "cdp_tab":
                violations.append("source_policy_relabeling_forbidden")

    return tuple(dict.fromkeys(violations))


# @anchor:wa.learn.procfs_memory.bind_descriptor_to_memory_sample
def bind_descriptor_to_memory_sample(
    descriptor: Mapping[str, Any],
    sample: MemorySample,
    *,
    source_policy: str,
) -> tuple[str, ...]:
    """Verify a target descriptor envelope against a `MemorySample` without source relabeling."""
    violations = list(schema.validate_target_descriptor(dict(descriptor)))
    state = descriptor.get("state") if isinstance(descriptor, Mapping) else None
    source = state.get("source") if isinstance(state, Mapping) else None
    state_kind = source.get("kind") if isinstance(source, Mapping) else None
    handle = descriptor.get("handle") if isinstance(descriptor, Mapping) else None
    handle_kind = (
        handle.get("kind")
        if isinstance(handle, Mapping)
        else getattr(handle, "kind", None)
    )
    handle_id = (
        handle.get("canonical_id")
        if isinstance(handle, Mapping)
        else getattr(handle, "canonical_id", None)
    )
    handle_gen = (
        handle.get("generation")
        if isinstance(handle, Mapping)
        else getattr(handle, "generation", None)
    )
    try:
        session_registry.Handle(handle_kind, handle_id, handle_gen)
    except session_registry.RegistryError as exc:
        violations.append(f"invalid_registry_handle:{exc}")
    if state_kind != source_policy:
        violations.append("descriptor_source_policy_relabeling_forbidden")
    violations.extend(
        validate_scope_isolation(source_policy=source_policy, handle_kind=handle_kind)
    )
    if isinstance(descriptor, Mapping) and descriptor.get("frame_seq") != sample.logical_tick:
        violations.append("descriptor_logical_tick_mismatch")
    return tuple(dict.fromkeys(violations))


# @anchor:wa.learn.procfs_memory.evaluate_peak_semantics
def evaluate_peak_semantics(
    *,
    instance_id: str,
    configured_limit_bytes: int | None,
    samples: tuple[MemorySample, ...] | list[MemorySample] = (),
    cgroup_peak_bytes: int | None = None,
    exclusive_ownership: bool = False,
    freshness_evidence: str | None = None,
    reported_peak_source: str = "cgroup_peak",
    python_allocation_peak_bytes: int | None = None,
    reserved_frame_bytes: int | None = None,
) -> PeakEvaluation:
    """Evaluate peak RAM claims under INV-J6-4 and Gate G-J6-4.

    - `cgroup_peak_bytes` certifies whole-instance lifetime peak ONLY when
      `exclusive_ownership is True` and `freshness_evidence` is a non-empty string
      and at least one complete sample exists.
    - Sampled PSS peak is a `"lower_bound_diagnostic"` (`sufficient_for_whole_instance_ram = False`).
    - `python_allocation_peak_bytes` and `reserved_frame_bytes` are
      `"optional_diagnostic_insufficient"` (`sufficient_for_whole_instance_ram = False`).
      Reporting either as whole-instance RAM is rejected as a gate violation.
    """
    _strict_str(instance_id, "instance_id")
    _strict_str(reported_peak_source, "reported_peak_source")
    violations: list[str] = []

    if configured_limit_bytes is None:
        violations.append("configured_limit_AUSENTE")
    else:
        _strict_int(configured_limit_bytes, "configured_limit_bytes", 1, MAX_MEMORY_BYTES)

    if cgroup_peak_bytes is not None:
        _strict_int(cgroup_peak_bytes, "cgroup_peak_bytes", 0, MAX_MEMORY_BYTES)
    if python_allocation_peak_bytes is not None:
        _strict_int(python_allocation_peak_bytes, "python_allocation_peak_bytes", 0, MAX_MEMORY_BYTES)
    if reserved_frame_bytes is not None:
        _strict_int(reserved_frame_bytes, "reserved_frame_bytes", 0, MAX_MEMORY_BYTES)

    complete_pss = [
        s.pss_total_bytes
        for s in samples
        if isinstance(s, MemorySample) and s.tree_complete and s.pss_total_bytes is not None
    ]
    pss_sampled_peak_bytes = max(complete_pss) if complete_pss else None

    sample_cgroup_peaks = [
        s.cgroup_peak_bytes
        for s in samples
        if isinstance(s, MemorySample) and s.cgroup_peak_bytes is not None
    ]
    effective_cgroup_peak = (
        cgroup_peak_bytes
        if cgroup_peak_bytes is not None
        else (max(sample_cgroup_peaks) if sample_cgroup_peaks else None)
    )

    fresh_ok = isinstance(freshness_evidence, str) and bool(freshness_evidence.strip())
    exclusive_ok = type(exclusive_ownership) is bool and exclusive_ownership is True

    diagnostics = {
        "pss_sampled_peak": {
            "bytes": pss_sampled_peak_bytes,
            "classification": "lower_bound_diagnostic",
            "sufficient_for_whole_instance_ram": False,
        },
        "python_allocation_peak": {
            "bytes": python_allocation_peak_bytes,
            "classification": "optional_diagnostic_insufficient",
            "sufficient_for_whole_instance_ram": False,
        },
        "reserved_frame_bytes": {
            "bytes": reserved_frame_bytes,
            "classification": "optional_diagnostic_insufficient",
            "sufficient_for_whole_instance_ram": False,
        },
    }

    if reported_peak_source == "python_allocation_peak":
        violations.append("python_peak_reported_as_instance_ram")
        classification = "optional_diagnostic_insufficient"
        sufficient = False
        reported_bytes = python_allocation_peak_bytes
    elif reported_peak_source == "reserved_frame_bytes":
        violations.append("reserved_frame_bytes_reported_as_instance_ram")
        classification = "optional_diagnostic_insufficient"
        sufficient = False
        reported_bytes = reserved_frame_bytes
    elif reported_peak_source == "pss_sampled_peak":
        classification = "lower_bound_diagnostic"
        sufficient = False
        reported_bytes = pss_sampled_peak_bytes
        if pss_sampled_peak_bytes is None:
            violations.append("pss_sampled_peak_AUSENTE")
    elif reported_peak_source == "pss_certified_as_lifetime_peak":
        violations.append("pss_peak_certified_as_lifetime_peak")
        classification = "lower_bound_diagnostic"
        sufficient = False
        reported_bytes = pss_sampled_peak_bytes
    elif reported_peak_source == "cgroup_peak":
        reported_bytes = effective_cgroup_peak
        if effective_cgroup_peak is None:
            violations.append("cgroup_peak_AUSENTE")
            classification = "absent"
            sufficient = False
        elif not exclusive_ok or not fresh_ok:
            violations.append("unscoped_or_stale_cgroup_peak")
            classification = "uncertified_cgroup_peak"
            sufficient = False
        else:
            classification = "certified_cgroup_lifetime_peak"
            sufficient = True
    else:
        raise MalformedProcfsError(
            f"unsupported reported_peak_source: {reported_peak_source!r}",
            code="malformed",
        )

    within_limit: bool | None = None
    if configured_limit_bytes is not None and reported_bytes is not None:
        within_limit = reported_bytes <= configured_limit_bytes
        if not within_limit:
            violations.append("peak_exceeds_configured_limit")
    if (
        configured_limit_bytes is not None
        and pss_sampled_peak_bytes is not None
        and pss_sampled_peak_bytes > configured_limit_bytes
    ):
        within_limit = False
        if "pss_lower_bound_exceeds_configured_limit" not in violations:
            violations.append("pss_lower_bound_exceeds_configured_limit")

    status = "ok" if not violations else "violation"
    return PeakEvaluation(
        instance_id=instance_id,
        peak_kind=reported_peak_source,
        classification=classification,
        reported_bytes=reported_bytes,
        configured_limit_bytes=configured_limit_bytes,
        sufficient_for_whole_instance_ram=sufficient,
        within_limit=within_limit,
        exclusive_ownership=exclusive_ok,
        freshness_evidence=freshness_evidence if fresh_ok else None,
        pss_sampled_peak_bytes=pss_sampled_peak_bytes,
        cgroup_peak_bytes=effective_cgroup_peak,
        diagnostics=diagnostics,
        violations=tuple(dict.fromkeys(violations)),
        status=status,
    )


# @anchor:wa.learn.procfs_memory.verify_prerequisite_receipts
def verify_prerequisite_receipts(
    receipts: Mapping[str, Any] | None,
    *,
    required_gates: tuple[str, ...] = ("G-M1-1", "G-M7-1", "G-M8-1"),
    required_revision: str | None = None,
) -> tuple[bool, dict[str, dict[str, Any]], tuple[str, ...]]:
    """Verify revision-bound earlier-wave owner gate receipts.

    Rejects missing receipts, expected failures (`expected_failure=True`),
    non-PASS statuses, and inferred consumer labels (`consumer_inferred=True`).
    """
    if required_revision is not None:
        _strict_str(required_revision, "required_revision")
    if not isinstance(receipts, Mapping) or not receipts:
        normalized = {
            g: {"status": "AUSENTE", "evidence": None, "owner_revision": None}
            for g in required_gates
        }
        return False, normalized, ("prerequisites_AUSENTE",)

    normalized_gates: dict[str, dict[str, Any]] = {}
    violations: list[str] = []
    for gate_id in required_gates:
        rec = receipts.get(gate_id)
        if not isinstance(rec, Mapping):
            normalized_gates[gate_id] = {
                "status": "AUSENTE",
                "evidence": None,
                "owner_revision": None,
            }
            violations.append(f"missing_prerequisite:{gate_id}")
            continue
        status = rec.get("status")
        evidence = rec.get("evidence")
        exp_fail = rec.get("expected_failure")
        owner_rev = rec.get("owner_revision")
        inferred = rec.get("consumer_inferred", False) or rec.get("source") == "consumer_inferred"

        if exp_fail is True:
            violations.append(f"expected_failure_not_accepted:{gate_id}")
        if inferred:
            violations.append(f"inferred_consumer_label_rejected:{gate_id}")
        if not isinstance(owner_rev, str) or not owner_rev:
            violations.append(f"missing_owner_revision:{gate_id}")
        elif required_revision is not None and owner_rev != required_revision:
            violations.append(f"revision_mismatch:{gate_id}")
        if status != "PASS" or not isinstance(evidence, str) or not evidence:
            violations.append(f"unproven_prerequisite:{gate_id}")

        gate_ok = not any(v.endswith(f":{gate_id}") for v in violations)
        normalized_gates[gate_id] = {
            "status": "PASS" if gate_ok else ("FAIL" if status == "FAIL" else "AUSENTE"),
            "evidence": evidence if isinstance(evidence, str) else None,
            "owner_revision": owner_rev if isinstance(owner_rev, str) else None,
            "expected_failure": bool(exp_fail),
            "consumer_inferred": bool(inferred),
        }

    return not violations, normalized_gates, tuple(dict.fromkeys(violations))


# @anchor:wa.learn.procfs_memory.validate_holdings_inline_fixtures
def validate_holdings_inline_fixtures(
    *,
    pause_command: Mapping[str, Any],
    outcome_record: Mapping[str, Any],
    training_enabled: bool = False,
    optional_training_status: str = "PASS",
) -> dict[str, Any]:
    """Validate inline Holdings command/outcome fixtures and optional training isolation.

    - A pause command has `tick`, `seq`, `cmd == "pause"`, and empty `args == {}`.
    - An outcome record has `seq`, `requested_tick`, `applied_tick`, `order_seq_id`,
      `status`, `reason`, and `state_sha256`.
    - Optional training failures never block disabled retail Holdings migration.
    """
    if not isinstance(pause_command, Mapping):
        raise MalformedProcfsError("pause_command must be a mapping", code="malformed")
    if set(pause_command.keys()) != {"tick", "seq", "cmd", "args"}:
        raise MalformedProcfsError(
            "pause_command must have exact keys {'tick', 'seq', 'cmd', 'args'}",
            code="malformed",
        )
    _strict_int(pause_command["tick"], "pause_command.tick", 0)
    _strict_int(pause_command["seq"], "pause_command.seq", 1)
    if pause_command["cmd"] != "pause":
        raise MalformedProcfsError("pause_command.cmd must be 'pause'", code="malformed")
    if not isinstance(pause_command["args"], Mapping) or dict(pause_command["args"]) != {}:
        raise MalformedProcfsError("pause_command.args must be empty {}", code="malformed")

    if not isinstance(outcome_record, Mapping):
        raise MalformedProcfsError("outcome_record must be a mapping", code="malformed")
    expected_outcome_keys = {
        "seq",
        "requested_tick",
        "applied_tick",
        "order_seq_id",
        "status",
        "reason",
        "state_sha256",
    }
    if set(outcome_record.keys()) != expected_outcome_keys:
        raise MalformedProcfsError(
            "outcome_record must have exact Holdings outcome keys",
            code="malformed",
        )
    _strict_int(outcome_record["seq"], "outcome_record.seq", 1)
    _strict_int(outcome_record["requested_tick"], "outcome_record.requested_tick", 0)
    _strict_int(outcome_record["applied_tick"], "outcome_record.applied_tick", 0)
    _strict_int(outcome_record["order_seq_id"], "outcome_record.order_seq_id", 1)
    if outcome_record["status"] not in ("accepted", "rejected", "proposed"):
        raise MalformedProcfsError("invalid Holdings outcome status", code="malformed")
    reason = outcome_record["reason"]
    if reason is not None and (not isinstance(reason, str) or not _HOLDINGS_REASON_RE.fullmatch(reason)):
        raise MalformedProcfsError("invalid Holdings outcome reason", code="malformed")
    sha = outcome_record["state_sha256"]
    if not isinstance(sha, str) or not _HEX64_RE.fullmatch(sha):
        raise MalformedProcfsError("invalid Holdings outcome state_sha256", code="malformed")

    if type(training_enabled) is not bool:
        raise MalformedProcfsError("training_enabled must be bool", code="malformed")
    _strict_str(optional_training_status, "optional_training_status")

    retail_admitted = outcome_record["status"] == "accepted" and (
        not training_enabled or optional_training_status == "PASS"
    )
    return {
        "retail_migration_admitted": retail_admitted,
        "training_enabled": training_enabled,
        "optional_training_status": optional_training_status,
        "optional_training_blocked_retail": False if not training_enabled else (optional_training_status != "PASS"),
        "pause_seq": pause_command["seq"],
        "outcome_order_seq_id": outcome_record["order_seq_id"],
    }


# @anchor:wa.learn.procfs_memory.assert_no_volatile_leakage
def assert_no_volatile_leakage(value: Any, path: str = "$") -> None:
    """Reject raw frames, numpy arrays, raw bytes, hook receipts, tree dumps, or checkpoint blobs."""
    if isinstance(value, (bytes, bytearray, memoryview, np.ndarray)):
        raise MalformedProcfsError(
            f"volatile binary or pixel payload forbidden at {path}",
            code="volatile_leakage",
        )
    if isinstance(value, float):
        raise MalformedProcfsError(f"float forbidden in bounded metadata at {path}", code="float_forbidden")
    if isinstance(value, Mapping):
        for k, v in value.items():
            if not isinstance(k, str):
                raise MalformedProcfsError(f"non-string key at {path}", code="malformed")
            if k in _VOLATILE_KEYS:
                raise MalformedProcfsError(
                    f"forbidden volatile field {k!r} at {path}.{k}",
                    code="volatile_leakage",
                )
            assert_no_volatile_leakage(v, f"{path}.{k}")
    elif isinstance(value, (tuple, list)):
        for i, item in enumerate(value):
            assert_no_volatile_leakage(item, f"{path}[{i}]")
    elif is_dataclass(value) and not isinstance(value, type):
        for f in fields(value):
            if f.name in _VOLATILE_KEYS:
                raise MalformedProcfsError(
                    f"forbidden volatile dataclass field {f.name!r} at {path}.{f.name}",
                    code="volatile_leakage",
                )
            assert_no_volatile_leakage(getattr(value, f.name), f"{path}.{f.name}")
    elif value is not None and type(value) not in (str, int, bool):
        raise MalformedProcfsError(
            f"unsupported value type {type(value).__name__} at {path}",
            code="malformed",
        )


def _to_plain(value: Any) -> Any:
    assert_no_volatile_leakage(value)
    if value is None or type(value) in (str, int, bool):
        return value
    if isinstance(value, Mapping):
        return {
            k: _to_plain(v)
            for k, v in value.items()
            if k not in _DIAGNOSTIC_TIMING_KEYS
        }
    if isinstance(value, (tuple, list)):
        return [_to_plain(v) for v in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _to_plain(getattr(value, f.name))
            for f in fields(value)
            if f.name not in _DIAGNOSTIC_TIMING_KEYS
        }
    raise MalformedProcfsError(f"unsupported canonical type: {type(value).__name__}", code="malformed")


# @anchor:wa.learn.procfs_memory.canonical_sample_bytes
def canonical_sample_bytes(record: Any) -> bytes:
    """Serialize a sample or gate report to canonical integer-only UTF-8 JSON without volatile leakage."""
    plain = _to_plain(record)
    return json.dumps(
        plain,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


# @anchor:wa.learn.procfs_memory.evaluate_j6_memory_gates
def evaluate_j6_memory_gates(
    *,
    instance_id: str,
    source_policy: str,
    handle_kind: str,
    budget_config: MemoryBudgetConfig | None,
    samples: tuple[MemorySample, ...] | list[MemorySample],
    overheads: OverheadScopes,
    peak_evaluation: PeakEvaluation,
    prerequisite_receipts: Mapping[str, Any] | None = None,
    required_revision: str | None = None,
    scope_checks: Mapping[str, Any] | None = None,
    holdings_migration: Mapping[str, Any] | None = None,
    metadata_envelope: Mapping[str, Any] | None = None,
) -> MemoryGateReport:
    """Evaluate all four J6 gates (`G-J6-1` .. `G-J6-4`) and return a `MemoryGateReport`."""
    _strict_str(instance_id, "instance_id")
    _strict_str(source_policy, "source_policy")
    _strict_str(handle_kind, "handle_kind")

    samples_tuple = tuple(samples)
    violations: list[str] = []

    if budget_config is None or not isinstance(budget_config, MemoryBudgetConfig):
        violations.append("budget_config_AUSENTE")

    if not samples_tuple:
        violations.append("samples_AUSENTE")

    if prerequisite_receipts is not None:
        prereq_ok, _, prereq_violations = verify_prerequisite_receipts(
            prerequisite_receipts,
            required_revision=required_revision,
        )
        if not prereq_ok:
            violations.extend(prereq_violations)

    if metadata_envelope is not None:
        assert_no_volatile_leakage(metadata_envelope)

    scope_cfg = dict(scope_checks or {})
    scope_violations = validate_scope_isolation(
        add_pss_and_cgroup=bool(scope_cfg.get("add_pss_and_cgroup", False)),
        cgroup_paths=tuple(scope_cfg.get("cgroup_paths", ())),
        tab_count=int(scope_cfg.get("tab_count", 1)),
        divide_shared_browser_by_tabs=bool(scope_cfg.get("divide_shared_browser_by_tabs", False)),
        source_policy=source_policy,
        handle_kind=handle_kind,
        merge_overhead_into_instance=bool(scope_cfg.get("merge_overhead_into_instance", False)),
    )
    violations.extend(scope_violations)

    if holdings_migration is not None:
        if holdings_migration.get("optional_training_blocked_retail") is True and not holdings_migration.get("training_enabled", False):
            violations.append("optional_training_blocked_disabled_retail_migration")
        if holdings_migration.get("retail_migration_admitted") is not True:
            violations.append("retail_holdings_migration_refused")

    # Check G-J6-1: Strict units and absence
    g1_violations: list[str] = []
    for s in samples_tuple:
        if s.instance_id != instance_id:
            g1_violations.append("cross_instance_sample_id")
        for p in s.processes:
            if p.status in ("missing", "unreadable") and p.pss_bytes is not None:
                g1_violations.append("missing_or_unreadable_pss_not_null")
            if p.pss_bytes is not None and p.pss_bytes < 0:
                g1_violations.append("negative_pss_bytes")
        for attr in ("cgroup_current_bytes", "cgroup_peak_bytes", "swap_current_bytes"):
            val = getattr(s, attr)
            if val is not None and val < 0:
                g1_violations.append(f"negative_{attr}")
            if budget_config is not None:
                if attr == "cgroup_current_bytes" and val is not None and val > budget_config.max_instance_ram_bytes:
                    g1_violations.append("cgroup_current_exceeds_limit")
                if attr == "cgroup_peak_bytes" and val is not None and val > budget_config.max_cgroup_peak_bytes:
                    g1_violations.append("cgroup_peak_exceeds_limit")
                if attr == "swap_current_bytes" and val is not None and val > budget_config.max_swap_bytes:
                    g1_violations.append("swap_current_exceeds_limit")
        if (
            budget_config is not None
            and s.pss_total_bytes is not None
            and s.pss_total_bytes > budget_config.max_instance_ram_bytes
        ):
            g1_violations.append("pss_total_exceeds_limit")

    # Check G-J6-2: Process identity, re-enumeration, and complete tree
    g2_violations: list[str] = []
    for s in samples_tuple:
        if not s.tree_complete or s.status != "ok":
            g2_violations.append(f"incomplete_process_tree:{s.status}")
        if s.gpu_accounting in ("missing_gpu_process", "unreadable_gpu_process"):
            g2_violations.append(f"gpu_accounting_incomplete:{s.gpu_accounting}")
        if budget_config is not None and (
            s.sample_skew_ns is None or s.sample_skew_ns > budget_config.max_sample_skew_ns
        ):
            g2_violations.append("sample_skew_exceeded")
        for p in s.processes:
            if p.identity is None or p.post_identity is None or p.identity != p.post_identity:
                g2_violations.append(f"identity_unstable:{p.pid}")

    # Check G-J6-3: Disjoint scopes and overhead separation
    g3_violations = list(scope_violations)
    if overheads.instance_id != instance_id:
        g3_violations.append("overhead_instance_mismatch")
    if overheads.instance_pss_bytes is None:
        g3_violations.append("instance_overhead_AUSENTE")

    # Check G-J6-4: Scoped peak semantics
    g4_violations = list(peak_evaluation.violations)
    if not peak_evaluation.sufficient_for_whole_instance_ram:
        g4_violations.append(f"peak_insufficient_for_instance_ram:{peak_evaluation.classification}")
    if peak_evaluation.within_limit is not True:
        g4_violations.append("peak_limit_not_satisfied")

    violations.extend(g1_violations)
    violations.extend(g2_violations)
    violations.extend(g3_violations)
    violations.extend(g4_violations)

    gates = {
        "G-J6-1": {
            "status": "PASS" if samples_tuple and not g1_violations and budget_config is not None else "FAIL",
            "evidence": "strict kB-to-bytes Pss and distinct cgroup byte quantities with null on absence",
            "violations": tuple(dict.fromkeys(g1_violations)),
            "scope": "injected_offline",
        },
        "G-J6-2": {
            "status": "PASS" if samples_tuple and not g2_violations and budget_config is not None else "FAIL",
            "evidence": "pre/post (pid, starttime, boot_id) re-enumeration across complete process tree",
            "violations": tuple(dict.fromkeys(g2_violations)),
            "scope": "injected_offline",
        },
        "G-J6-3": {
            "status": "PASS" if samples_tuple and not g3_violations and budget_config is not None else "FAIL",
            "evidence": "separate PSS/cgroup regimes, non-overlapping cgroup scopes, no tab division, separate overheads",
            "violations": tuple(dict.fromkeys(g3_violations)),
            "scope": "injected_offline",
        },
        "G-J6-4": {
            "status": "PASS" if samples_tuple and not g4_violations and budget_config is not None else "FAIL",
            "evidence": "exclusive fresh cgroup lifetime peak vs PSS lower-bound and insufficient Python/frame diagnostics",
            "violations": tuple(dict.fromkeys(g4_violations)),
            "scope": "injected_offline",
        },
    }

    absent = {
        "conductor_admission_binding": "AUSENTE",
        "live_procfs_sampling": "AUSENTE",
        "measured_host_ram": "AUSENTE",
        "timing_label": "VM",
    }
    deduped_violations = tuple(dict.fromkeys(violations))
    passed = (
        not deduped_violations
        and all(g["status"] == "PASS" for g in gates.values())
    )
    return MemoryGateReport(
        instance_id=instance_id,
        source_policy=source_policy,
        samples=samples_tuple,
        overheads=overheads,
        peak_evaluation=copy.deepcopy(peak_evaluation),
        gates=gates,
        violations=deduped_violations,
        absent=absent,
        passed=passed,
    )


__all__ = (
    "BYTES_PER_KB",
    "MAX_MEMORY_BYTES",
    "CHROMIUM_ROLES",
    "OVERHEAD_SCOPES",
    "SOURCE_POLICIES",
    "REQUIRED_J6_GATES",
    "GPU_ACCOUNTING_MODES",
    "PEAK_CLASSIFICATIONS",
    "ProcfsMemoryError",
    "MissingProcfsError",
    "UnreadableProcfsError",
    "MalformedProcfsError",
    "SmapsRollup",
    "ProcStat",
    "ProcessIdentity",
    "ProcessSample",
    "MemorySample",
    "ProcfsMemorySample",
    "OverheadScopes",
    "PeakEvaluation",
    "MemoryBudgetConfig",
    "MemoryGateReport",
    "parse_smaps_rollup",
    "probe_smaps_rollup",
    "parse_cgroup_bytes",
    "probe_cgroup_memory",
    "parse_boot_id",
    "parse_proc_stat",
    "parse_stat",
    "parse_procfs_stat",
    "parse_process_identity",
    "classify_process_role",
    "sample_process_tree",
    "validate_scope_isolation",
    "bind_descriptor_to_memory_sample",
    "evaluate_peak_semantics",
    "verify_prerequisite_receipts",
    "validate_holdings_inline_fixtures",
    "assert_no_volatile_leakage",
    "canonical_sample_bytes",
    "evaluate_j6_memory_gates",
)
