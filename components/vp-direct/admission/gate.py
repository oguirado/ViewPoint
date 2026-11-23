"""Reserve capacity across the surface owner's check and allocation."""

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock, local
from typing import TYPE_CHECKING

from perception.baseline.inventory import IncompleteInventory, collect
from schemas.validation import validate_contract

if TYPE_CHECKING:
    from perception.stream import PerceptionStream


MAX_SURFACES = 16  # Same ceiling as daemon-adapter/internal/daemon/environment.go.
MIN_MARGIN_PERCENT = 15  # TARGET, vp.admission/v1.
_RETAINED_STATES = {"ASLEEP", "OFF", "QUARANTINED"}
_reservation = local()


class AdmissionError(ValueError):
    """Required accounting evidence was absent or inconsistent."""


def _track_file_stream(stream, surface_id):
    """Enroll a D3 allocation under the gate holding its reservation."""
    active = getattr(_reservation, "active", None)
    if active is None:
        return
    gate, reserved_surface_id, admitted = active
    if not admitted or surface_id != reserved_surface_id:
        raise AdmissionError("file sequence requires an admitted reservation for its surface")
    gate._file_streams.add(stream)


def _byte_count(value):
    return type(value) is int and value >= 0


@dataclass(frozen=True)
class ResourceUsage:
    """One allocation, as reported by its P3, P5 or D3 owner.

    Pending bytes are commitments still to be allocated. Retained bytes are a
    subset of the reservation, with any outstanding commitments still pending.
    Both fields describe the same allocation and must never be added together.
    """

    resource_id: str
    reserved_bytes: int
    pending_bytes: int
    retained_bytes: int

    def __post_init__(self):
        if (type(self.resource_id) is not str or not self.resource_id.strip()
                or not all(_byte_count(value) for value in (
                self.reserved_bytes, self.pending_bytes, self.retained_bytes))
                or max(self.pending_bytes, self.retained_bytes) > self.reserved_bytes):
            raise AdmissionError("invalid allocation accounting")


@dataclass(frozen=True)
class UsageSnapshot:
    """All incumbent surfaces, their P3 rings, and other allocations once each.

    An open surface counts even when asleep, off or hidden. Use None for a
    surface with no P3 ring. Supply stream-owned surfaces via streams for live
    ring accounting. The gate also tracks streams with unsettled file sequences
    across snapshots. List other resources once per allocation and keep them
    here until their owner verifies settlement.
    """

    surfaces: Mapping[str, ResourceUsage | None]
    other_resources: tuple[ResourceUsage, ...] = ()
    streams: tuple["PerceptionStream", ...] = ()


@dataclass(frozen=True)
class AdmissionPlan:
    """An owner-provisioned state and backend with its incremental costs."""

    surface_id: str
    state: str
    backend: str
    gpu_dependent: bool
    new_ring_bytes: int = 0
    other_new_bytes: int = 0

    def __post_init__(self):
        if (type(self.gpu_dependent) is not bool
                or not _byte_count(self.new_ring_bytes) or not _byte_count(self.other_new_bytes)):
            raise AdmissionError("invalid plan accounting")


def _read_host_memory():
    """Read the kernel's MemTotal and MemAvailable from the same sample."""
    fields = {}
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        key, _, value = line.partition(":")
        if key in ("MemTotal", "MemAvailable"):
            words = value.split()
            if key in fields or len(words) != 2 or not words[0].isdecimal() or words[1] != "kB":
                raise AdmissionError("invalid host memory sample")
            fields[key] = int(words[0]) * 1024
    if fields.keys() != {"MemTotal", "MemAvailable"}:
        raise AdmissionError("incomplete host memory sample")
    return fields["MemTotal"], fields["MemAvailable"]


def _pending_bytes(resources):
    return sum(resource.pending_bytes for resource in resources)


class AdmissionGate:
    """Single-owner, in-process gate for ViewPoint's perception acquisitions.

    Use one gate for the owner and hold the reservation through the allocation
    and its accounting update. The owner supplies its complete resource view
    under this gate, including unresolved allocations after a failed attempt.
    It also supplies the full set of disjoint cgroup scopes to P1. Do not use a
    decision outside its reservation scope as a permit to allocate.
    """

    def __init__(self, read_usage, *, scopes, read_memory=_read_host_memory, inventory=collect):
        self._read_usage = read_usage
        self._scopes = tuple(scopes)
        if not self._scopes:
            raise AdmissionError("the owner's cgroup scopes are required")
        self._read_memory = read_memory
        self._inventory = inventory
        self._lock = Lock()
        self._file_streams: set["PerceptionStream"] = set()

    @contextmanager
    def reserve(self, plan):
        """Yield the frozen admission receipt while competing allocations wait."""
        with self._lock:
            decision = self._decision(plan)
            previous = getattr(_reservation, "active", None)
            _reservation.active = self, plan.surface_id, decision["admitted"]
            try:
                yield decision
            finally:
                _reservation.active = previous

    def _decision(self, plan):
        usage = self._read_usage()
        surfaces = dict(usage.surfaces)
        file_sequences = []
        target_files = []
        sampled_streams = set(usage.streams)
        tracked_only = self._file_streams - sampled_streams
        for stream in (*usage.streams, *tracked_only):
            accounting = stream.accounting()
            if accounting.file_sequences:
                self._file_streams.add(stream)
            else:
                self._file_streams.discard(stream)
                if stream in tracked_only:
                    continue
            surface_id = accounting.surface_id
            if stream in sampled_streams:
                if surface_id in surfaces:
                    raise AdmissionError("overlapping surface accounting")
                surfaces[surface_id] = accounting.ring
            else:
                # A retired stream can have D3 charges alongside a current
                # P3 stream for the same surface. Count the surface and ring once.
                reported_ring = surfaces.setdefault(surface_id, accounting.ring)
                if accounting.ring is not None:
                    if reported_ring is not None and reported_ring != accounting.ring:
                        raise AdmissionError("overlapping surface accounting")
                    surfaces[surface_id] = accounting.ring
            file_sequences.extend(accounting.file_sequences)
            if surface_id == plan.surface_id:
                target_files.extend(accounting.file_sequences)
        rings = tuple(ring for ring in surfaces.values() if ring is not None)
        resources = rings + tuple(file_sequences) + usage.other_resources
        if len({resource.resource_id for resource in resources}) != len(resources):
            raise AdmissionError("overlapping resource reservations")

        try:
            inventory = self._inventory(self._scopes)
            total, available = self._read_memory()
        except (IncompleteInventory, OSError, UnicodeError) as error:
            raise AdmissionError("host or cgroup memory evidence unavailable") from error
        resident = inventory.get("charged_bytes")
        if inventory.get("complete") is not True or not _byte_count(resident):
            raise AdmissionError("complete cgroup memory evidence required")
        if not _byte_count(total) or not _byte_count(available) or total == 0 or available > total:
            raise AdmissionError("invalid host memory sample")

        old_ring = surfaces.get(plan.surface_id)
        old_bytes = old_ring.reserved_bytes if old_ring is not None else 0
        retained = sum(resource.retained_bytes for resource in resources)
        count = len(surfaces) + (plan.surface_id not in surfaces)
        # MemAvailable already reflects current allocations. P1's memory.current
        # is the resident charge, and its per-PID PSS explains that charge.
        remaining = available - _pending_bytes(resources) - plan.new_ring_bytes - plan.other_new_bytes
        adequate_margin = 100 * remaining >= MIN_MARGIN_PERCENT * total
        # P1 has no qualified GPU accounting source for this gate.
        admitted = count <= MAX_SURFACES and adequate_margin and not plan.gpu_dependent
        if admitted and plan.state in _RETAINED_STATES:
            if old_ring is not None:
                retained += old_bytes - old_ring.retained_bytes
            retained += sum(resource.reserved_bytes - resource.retained_bytes for resource in target_files)

        decision = {
            "schema": "vp.admission/v1",
            "surface_id": plan.surface_id,
            "state": plan.state,
            "backend": plan.backend,
            "admitted": admitted,
            "surface_count": count,
            "resident_bytes": resident,
            "retained_bytes": retained,
            "old_ring_bytes": old_bytes,
            "new_ring_bytes": plan.new_ring_bytes,
            "reserved_peak_bytes": old_bytes + plan.new_ring_bytes,
            "available_bytes": available,
            "margin_percent": 100 * max(remaining, 0) / total,
            "gpu_dependent": plan.gpu_dependent,
            "gpu_accounted": False,
        }
        validate_contract("vp.admission/v1", decision)
        return decision
