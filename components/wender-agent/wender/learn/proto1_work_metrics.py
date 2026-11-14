# @module {"id":"wa.learn.proto1_work_metrics","layer":2,"owner":"LEARN","deps":["wa.act.window_scheduler","wa.act.budget_governor","wa.act.focus_policy","wa.act.target_lock_registry","wa.perceive.session_registry"],"book":"HB-WA-s4-learn"}
"""Portable per-window work/latency telemetry (C-budget, C-score prerequisites).

Instrumentation is VM/offline evidence, never a live-host qualification. The
nearest-rank estimator for p-th percentile is sorted[ceil(p*n/100)-1]; mean
uses integer floor division. No interpolation, rounding or floating-point time.
Injected scheduling clocks are never used as evidence for CPU usage.

Frame content exists only as immutable, owned RAM; reports contain identities,
digests, counts and timing distributions, not pixels or encoded frames.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import re
import threading
import time
from typing import Callable, Mapping


STAGES = ("queue_wait", "capture", "description", "scheduler_service",
          "provider", "action_ack", "live_app")
CPU_STAGES = frozenset(STAGES) - {"queue_wait"}
HOST_TARGETS_NS = (
    ("capture_to_description_p95_ns", 50_000_000),
    ("cpu_per_processed_frame_mean_ns", 8_000_000),
    ("scheduler_round_p95_ns", 1_000_000),
    ("offline_transport_round_p95_ns", 100_000_000),
)
_SHA64 = re.compile(r"[0-9a-f]{64}\Z")
_SHA40 = re.compile(r"[0-9a-f]{40}\Z")


class MetricsRefused(ValueError):
    """A measurement, identity, population or bounded-work contract failed."""


def _int(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise MetricsRefused(f"{label} must be int >= {minimum}")
    return value


def _name(value, label):
    if type(value) is not str or not value or value != value.strip():
        raise MetricsRefused(f"{label} must be a nonempty canonical string")
    return value


def handle_identity(handle):
    """Common C boundary: structural kind, canonical_id, generation IN ORDER."""
    try:
        kind, ident, generation = handle.kind, handle.canonical_id, handle.generation
    except AttributeError as exc:
        raise MetricsRefused("AUSENTE: structural Handle") from exc
    if kind not in ("cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"):
        raise MetricsRefused("unknown target kind")
    return (_name(kind, "kind"), _name(ident, "canonical_id"),
            _int(generation, "generation", 1))


def nearest_rank(values, percentile):
    """Deterministic ceil(percentile*n/100), one-based, then sorted rank."""
    _int(percentile, "percentile", 1)
    if percentile > 100:
        raise MetricsRefused("percentile > 100")
    seq = list(values)
    if not seq:
        raise MetricsRefused("AUSENTE: empty sample population")
    for number in seq:
        _int(number, "sample")
    return sorted(seq)[(percentile * len(seq) + 99) // 100 - 1]


def distribution(values):
    """Exact integer distribution, sorted samples retained for independent audit."""
    seq = sorted(values)
    if not seq:
        raise MetricsRefused("AUSENTE: empty sample population")
    for value in seq:
        _int(value, "duration")
    n = len(seq)
    return {"count": n, "min": seq[0], "mean": sum(seq) // n,
            "p50": seq[(50*n + 99)//100 - 1],
            "p95": seq[(95*n + 99)//100 - 1],
            "p99": seq[(99*n + 99)//100 - 1],
            "max": seq[-1], "samples_ns": seq}


@dataclass(frozen=True)
class MetricRecord:
    """Exact ordered record: name,value,unit,evidence,status (no extra fields).

    No MEASURED claim is legal without a real integer value and a declared
    clock provenance. Missing values use CITED with an AUSENTE explanation,
    not a zero or a fabricated MEASURED flag.
    """
    name: str
    value: int | None
    unit: str
    evidence: str
    status: str

    def __post_init__(self):
        _name(self.name, "metric name")
        _name(self.unit, "metric unit")
        _name(self.evidence, "metric evidence")
        if self.status not in ("MEASURED", "CITED", "TARGET"):
            raise MetricsRefused("invalid metric status")
        if self.value is None:
            if self.status != "CITED" or not self.evidence.startswith("AUSENTE:"):
                raise MetricsRefused("null metric must be CITED with AUSENTE reason")
        else:
            _int(self.value, "metric value")
        if self.status == "MEASURED" and "clock:monotonic_ns" not in self.evidence:
            raise MetricsRefused("fabricated MEASURED flag: clock evidence absent")

    def serialise(self):
        return asdict(self)


@dataclass(frozen=True)
class FramePin:
    """Immutable in-RAM canonical RGB24 and its independently checked digest."""
    handle: tuple[str, str, int]
    epoch: int
    frame_seq: int
    width: int
    height: int
    rgb24: bytes
    digest: str
    source_sha256: str

    @classmethod
    def from_frame(cls, frame, *, source_sha256):
        if type(source_sha256) is not str or _SHA64.fullmatch(source_sha256) is None:
            raise MetricsRefused("missing source file SHA-256 pin")
        ident = handle_identity(frame.handle)
        epoch = _int(frame.epoch, "epoch")
        seq = _int(frame.frame_seq, "frame_seq")
        width = _int(frame.width, "width", 1)
        height = _int(frame.height, "height", 1)
        rgb = frame.rgb
        # NumPy is already required by the canonical registry. Keep import local
        # so pure clock and budget utilities remain stdlib-only.
        import numpy as np
        if (not isinstance(rgb, np.ndarray) or rgb.dtype != np.dtype("uint8")
                or rgb.shape != (height, width, 3) or not rgb.flags.c_contiguous):
            raise MetricsRefused("capture must be contiguous HxWx3 uint8 RGB24")
        owned = bytes(rgb.tobytes(order="C"))
        if len(owned) != width * height * 3:
            raise MetricsRefused("RGB24 length mismatch")
        digest = sha256(owned).hexdigest()
        if type(frame.digest) is not str or digest != frame.digest:
            raise MetricsRefused("torn frame: SHA-256 mismatch")
        return cls(ident, epoch, seq, width, height, owned, digest, source_sha256)

    def __post_init__(self):
        if (type(self.handle) is not tuple or len(self.handle) != 3
                or self.handle[0] not in ("cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window")):
            raise MetricsRefused("capture handle shape is invalid")
        _name(self.handle[1], "capture canonical_id")
        _int(self.handle[2], "capture generation", 1)
        _int(self.epoch, "epoch")
        _int(self.frame_seq, "frame_seq")
        _int(self.width, "width", 1)
        _int(self.height, "height", 1)
        if type(self.rgb24) is not bytes or len(self.rgb24) != self.width*self.height*3:
            raise MetricsRefused("immutable RGB24 RAM ownership required")
        if self.digest != sha256(self.rgb24).hexdigest():
            raise MetricsRefused("canonical capture digest mismatch")
        if type(self.source_sha256) is not str or not _SHA64.fullmatch(self.source_sha256):
            raise MetricsRefused("capture source SHA missing")

    def metadata(self):
        return {"handle": list(self.handle), "epoch": self.epoch,
                "frame_seq": self.frame_seq, "width": self.width,
                "height": self.height, "digest": self.digest,
                "source_sha256": self.source_sha256}


@dataclass(frozen=True)
class Sample:
    """One real, monotonic stage interval; CPU fields absent for queue wait."""
    handle: tuple[str, str, int]
    epoch: int
    frame_seq: int
    workload: str
    stage: str
    wall_start_ns: int
    wall_end_ns: int
    cpu_start_ns: int | None
    cpu_end_ns: int | None
    operations: int
    source_sha256: str

    def __post_init__(self):
        if self.stage not in STAGES:
            raise MetricsRefused("unknown measurement stage")
        if (type(self.handle) is not tuple or len(self.handle) != 3
                or self.handle[0] not in ("cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window")):
            raise MetricsRefused("sample Handle identity missing")
        _name(self.handle[1], "sample identity")
        _int(self.handle[2], "generation", 1)
        _int(self.epoch, "epoch")
        _int(self.frame_seq, "frame_seq")
        _name(self.workload, "workload")
        _int(self.wall_start_ns, "start monotonic")
        _int(self.wall_end_ns, "end monotonic")
        if self.wall_end_ns < self.wall_start_ns:
            raise MetricsRefused("reversed wall timestamps / negative duration")
        if self.cpu_start_ns is None or self.cpu_end_ns is None:
            if self.stage != "queue_wait" or self.cpu_start_ns is not None or self.cpu_end_ns is not None:
                raise MetricsRefused("CPU clock missing from service interval")
        else:
            _int(self.cpu_start_ns, "start CPU")
            _int(self.cpu_end_ns, "end CPU")
            if self.cpu_end_ns < self.cpu_start_ns:
                raise MetricsRefused("reversed CPU timestamps / negative stage duration")
        _int(self.operations, "operations")
        if type(self.source_sha256) is not str or not _SHA64.fullmatch(self.source_sha256):
            raise MetricsRefused("source SHA missing")

    @property
    def wall_ns(self):
        return self.wall_end_ns - self.wall_start_ns

    @property
    def cpu_ns(self):
        return None if self.cpu_end_ns is None else self.cpu_end_ns - self.cpu_start_ns


@dataclass(frozen=True)
class QueueMark:
    handle: tuple[str, str, int]
    epoch: int
    frame_seq: int
    workload: str
    start_ns: int
    source_sha256: str


class WorkSampler:
    """Measured clocks are fixed real clocks; scheduling clocks may be injected
    into *other* objects but never into this sampler or its CPU evidence.

    ``report(population=...)`` requires an explicit complete seven-stage census.
    Missing optional stages are CITED/AUSENTE, and empty required stages FAIL.
    """

    def __init__(self, *, source_commit, source_sha256, workload, affinity,
                 max_operations=100_000, max_samples=10_000, max_windows=4,
                 max_bytes=4*640*360*3):
        if type(source_commit) is not str or not _SHA40.fullmatch(source_commit):
            raise MetricsRefused("source commit must be 40 lowercase hex")
        if type(source_sha256) is not str or not _SHA64.fullmatch(source_sha256):
            raise MetricsRefused("source_sha256 must be 64 lowercase hex")
        self.source_commit = source_commit
        self.source_sha256 = source_sha256
        self.workload = _name(workload, "workload")
        self.affinity = _name(affinity, "affinity")
        self.max_operations = _int(max_operations, "max_operations", 1)
        self.max_samples = _int(max_samples, "max_samples", 1)
        self.max_windows = _int(max_windows, "max_windows", 1)
        self.max_bytes = _int(max_bytes, "max_bytes", 1)
        self._samples = []
        self._frames = {}
        self._operations = 0
        self._lock = threading.RLock()
        self._cpu_clock_name = ("thread_time_ns" if callable(getattr(time, "thread_time_ns", None))
                                else "process_time_ns")
        self._cpu_clock = getattr(time, self._cpu_clock_name)

    def pin(self, frame):
        pin = FramePin.from_frame(frame, source_sha256=self.source_sha256)
        key = (pin.handle, pin.epoch, pin.frame_seq)
        with self._lock:
            if key in self._frames:
                if self._frames[key].digest != pin.digest:
                    raise MetricsRefused("same identity changed frame digest")
                return self._frames[key]
            if len({p.handle for p in self._frames.values()} | {pin.handle}) > self.max_windows:
                raise MetricsRefused("C-budget: too many active windows")
            if sum(len(p.rgb24) for p in self._frames.values()) + len(pin.rgb24) > self.max_bytes:
                raise MetricsRefused("C-budget: RGB RAM byte cap exceeded")
            self._frames[key] = pin
        return pin

    def _check_pin(self, pin):
        if type(pin) is not FramePin:
            raise MetricsRefused("canonical FramePin required")
        key = (pin.handle, pin.epoch, pin.frame_seq)
        with self._lock:
            if self._frames.get(key) is not pin or pin.source_sha256 != self.source_sha256:
                raise MetricsRefused("foreign/mixed frame identity or source")

    def _append(self, sample):
        with self._lock:
            if sample.workload != self.workload or sample.source_sha256 != self.source_sha256:
                raise MetricsRefused("mixed workload identity")
            if not any((sample.handle, sample.epoch, sample.frame_seq) == k for k in self._frames):
                raise MetricsRefused("sample has no pinned RGB frame")
            if len(self._samples) >= self.max_samples:
                raise MetricsRefused("C-budget: sample cap exceeded")
            if sample.operations + self._operations > self.max_operations:
                raise MetricsRefused("C-budget: operation cap exceeded")
            self._operations += sample.operations
            self._samples.append(sample)
        return sample

    def measure(self, stage, pin, operation: Callable, *, operations=0):
        """Run real CPU work, return (result, Sample) with real wall/CPU stamps."""
        if stage not in CPU_STAGES:
            raise MetricsRefused("use queue_start/queue_end for queue wait")
        self._check_pin(pin)
        _int(operations, "operations")
        with self._lock:
            if self._operations + operations > self.max_operations:
                raise MetricsRefused("C-budget: deliberate excessive work refused")
            if len(self._samples) >= self.max_samples:
                raise MetricsRefused("C-budget: sample cap exceeded")
        if not callable(operation):
            raise MetricsRefused("operation must be callable")
        wall_start = time.monotonic_ns()
        cpu_start = self._cpu_clock()
        try:
            result = operation()
        finally:
            cpu_end = self._cpu_clock()
            wall_end = time.monotonic_ns()
        sample = Sample(pin.handle, pin.epoch, pin.frame_seq, self.workload,
                        stage, wall_start, wall_end, cpu_start, cpu_end,
                        operations, self.source_sha256)
        self._append(sample)
        return result, sample

    def queue_start(self, pin):
        self._check_pin(pin)
        return QueueMark(pin.handle, pin.epoch, pin.frame_seq, self.workload,
                         time.monotonic_ns(), self.source_sha256)

    def queue_end(self, pin, mark):
        self._check_pin(pin)
        if type(mark) is not QueueMark or (
                mark.handle, mark.epoch, mark.frame_seq, mark.workload,
                mark.source_sha256) != (pin.handle, pin.epoch, pin.frame_seq,
                                        self.workload, self.source_sha256):
            raise MetricsRefused("queue mark identity does not match")
        end_ns = time.monotonic_ns()
        return self._append(Sample(pin.handle, pin.epoch, pin.frame_seq,
            self.workload, "queue_wait", mark.start_ns, end_ns,
            None, None, 0, self.source_sha256))

    @property
    def samples(self):
        with self._lock:
            return tuple(self._samples)

    def report(self, *, population, required_stages=("capture", "description")):
        """Strict census: all seven stage names, integer counts matching samples."""
        if (not isinstance(population, dict) or set(population) != set(STAGES)):
            raise MetricsRefused("AUSENTE: complete explicit stage population")
        if not isinstance(required_stages, tuple) or not required_stages or (
                any(stage not in STAGES for stage in required_stages)):
            raise MetricsRefused("invalid nonempty required stage population")
        samples = self.samples
        if not samples:
            raise MetricsRefused("AUSENTE: empty sample sets")
        counts = {s: sum(x.stage == s for x in samples) for s in STAGES}
        for stage in STAGES:
            _int(population[stage], f"population[{stage}]")
            if population[stage] != counts[stage]:
                raise MetricsRefused("population mismatch for " + stage)
        if any(counts[s] == 0 for s in required_stages):
            raise MetricsRefused("AUSENTE: empty required sample population")
        if (len({x.workload for x in samples}) != 1
                or len({x.source_sha256 for x in samples}) != 1):
            raise MetricsRefused("mixed workload identities")
        metrics, absent, distributions = [], {}, {}
        evidence = f"VM clock:monotonic_ns+{self._cpu_clock_name}; workload:{self.workload}"
        for stage in STAGES:
            cohort = [x for x in samples if x.stage == stage]
            if not cohort:
                absent[stage] = "AUSENTE: no eligible samples"
                metrics.append(MetricRecord(stage+"_wall_p95_ns", None, "ns",
                                            absent[stage], "CITED").serialise())
                continue
            wall = distribution([x.wall_ns for x in cohort])
            cpu = distribution([x.cpu_ns for x in cohort if x.cpu_ns is not None]) if stage != "queue_wait" else None
            distributions[stage] = {"wall": wall, "cpu": cpu}
            metrics.append(MetricRecord(stage+"_wall_p95_ns", wall["p95"], "ns",
                                        evidence, "MEASURED").serialise())
            if cpu is not None:
                metrics.append(MetricRecord(stage+"_cpu_mean_ns", cpu["mean"], "ns",
                                            evidence, "MEASURED").serialise())
        # Capture->description span: explicit matched handle/epoch/frame_seq;
        # independently match stamps instead of adding unrelated stage p95s.
        by_id = {}
        for sample in samples:
            by_id.setdefault((sample.handle, sample.epoch, sample.frame_seq), {}).setdefault(sample.stage, []).append(sample)
        capture_to_description = []
        cpu_per_frame = []
        for stage_map in by_id.values():
            cap, desc = stage_map.get("capture", []), stage_map.get("description", [])
            if len(cap) == 1 and len(desc) == 1:
                if desc[0].wall_start_ns < cap[0].wall_end_ns:
                    raise MetricsRefused("reversed capture/description timestamps")
                capture_to_description.append(desc[0].wall_end_ns-cap[0].wall_start_ns)
                # CPU service totals belong to the same processed frame, not to
                # injected queue clocks or provider/app latency populations.
                cpu_per_frame.append(sum(x.cpu_ns for stage in
                    ("capture", "description", "scheduler_service", "action_ack")
                    for x in stage_map.get(stage, [])))
        if capture_to_description:
            distributions["capture_to_description"] = distribution(capture_to_description)
            distributions["cpu_per_processed_frame"] = distribution(cpu_per_frame)
            metrics.append(MetricRecord("capture_to_description_p95_ns",
                 nearest_rank(capture_to_description, 95), "ns", evidence, "MEASURED").serialise())
            metrics.append(MetricRecord("cpu_per_processed_frame_mean_ns",
                 sum(cpu_per_frame)//len(cpu_per_frame), "ns", evidence, "MEASURED").serialise())
        else:
            absent["capture_to_description"] = "AUSENTE: no paired complete capture/description"
        # Host thresholds are ALWAYS target metadata, never an offline pass.
        targets = [MetricRecord(name, value, "ns", "four live 640x360 RGB24 windows; host TARGET", "TARGET").serialise()
                   for name, value in HOST_TARGETS_NS]
        return {
            "source_pin": {"ref_commit": self.source_commit, "source_sha256": self.source_sha256},
            "workload": self.workload, "affinity": self.affinity, "scope": "VM/offline",
            "sample_population": dict(population), "frame_population": len(self._frames),
            "eligible_visits": None, "eligible_visits_reason": "AUSENTE: qualified live-host visits not observed",
            "cpu_clock": self._cpu_clock_name,
            "frame_metadata": [p.metadata() for p in self._frames.values()],
            "operations": self._operations, "bounded": self._operations <= self.max_operations,
            "distributions": distributions, "metrics": metrics, "absent": absent,
            "host_targets": targets, "host_qualification": "AUSENTE",
            "provider_population": counts["provider"],
            "live_app_population": counts["live_app"],
        }


def bounded_cpu_work(iterations, *, limit=50_000, seed=0x9e3779b9):
    """Deterministic genuine integer CPU work, no sleep, I/O or fake duration."""
    _int(iterations, "iterations")
    _int(limit, "limit", 1)
    _int(seed, "seed")
    if iterations > limit:
        raise MetricsRefused("C-budget: deliberate excessive work refused")
    value = seed
    for index in range(iterations):
        value ^= (value << 13) & 0xffffffff
        value ^= value >> 17
        value ^= (value << 5) & 0xffffffff
        value = (value + index) & 0xffffffff
    return value


class _RegistryPort:
    """Strict translation of the actual SessionRegistry.resolve API."""
    def __init__(self, registry):
        from wender.perceive.session_registry import SessionRegistry
        if not isinstance(registry, SessionRegistry):
            raise MetricsRefused("real SessionRegistry required")
        self._real = registry

    def get(self, handle):
        from wender.perceive.session_registry import StaleHandleError
        try:
            return self._real.resolve(handle)
        except StaleHandleError:
            return None

    def resolve(self, handle):
        return self._real.resolve(handle)

    def canonical_resource(self, handle):
        record = self._real.resolve(handle)
        # A shared canonical_id is the only admitted cross-kind alias proof.
        return record.handle.canonical_id


class _LockPort:
    def __init__(self, real):
        from wender.act.target_lock_registry import TargetLockRegistry
        if not isinstance(real, TargetLockRegistry):
            raise MetricsRefused("real TargetLockRegistry required")
        self.real = real
        self._inflight = set()
        self._acknowledged = set()

    def try_acquire(self, handle, *, owner, expires_ns):
        from wender.act.target_lock_registry import TargetBusy
        try:
            return self.real.acquire(handle, owner, expires_ns=expires_ns)
        except TargetBusy:
            return None

    def mark_inflight(self, lease):
        self.real.mark_inflight(lease)
        self._inflight.add(id(lease))

    def acknowledge(self, lease):
        if id(lease) not in self._inflight:
            raise MetricsRefused("no actual send for acknowledgment")
        self._acknowledged.add(id(lease))

    def release(self, lease):
        key = id(lease)
        if key in self._inflight:
            if key not in self._acknowledged:
                raise MetricsRefused("inflight lease lacks positive quiescence")
            self.real.reconcile_quarantine(lease.handle, acknowledged=True,
                                           expected_lease=lease)
            self._inflight.remove(key)
            self._acknowledged.remove(key)
        else:
            self.real.release(lease)

    def quarantine(self, lease):
        if id(lease) in self._inflight:
            self.real.reconcile_quarantine(lease.handle, acknowledged=False,
                                           expected_lease=lease)
        else:
            # Never falsely claim an unsent operation was sent or releasable.
            raise MetricsRefused("unsent quarantine requires separate reconciliation")


class _FocusPort:
    def __init__(self, real, *, domain):
        from wender.act.focus_policy import FocusPolicy
        if not isinstance(real, FocusPolicy):
            raise MetricsRefused("real FocusPolicy required")
        self.real = real
        self.domain = _name(domain, "focus domain")
        self._consumed = set()

    def try_acquire(self, lease, *, epoch):
        from wender.act.focus_policy import FocusRefused
        if self.real.registry.get(lease.handle).epoch != epoch:
            return None
        try:
            return self.real.grant(lease, domain=self.domain)
        except FocusRefused:
            return None

    def consume(self, focus, observation):
        self.real.validate(focus.lease, focus, observation, domain=self.domain)
        self.real.consume(focus)
        self._consumed.add(id(focus))

    def release(self, focus):
        if id(focus) in self._consumed:
            self._consumed.remove(id(focus))
        else:
            # Explicitly destroy an unused grant, not manufacture a send.
            self.real.consume(focus)

    def quarantine(self, focus):
        # A consumed focus grant already fails any subsequent validation.
        if id(focus) not in self._consumed:
            self.real.consume(focus)
            self._consumed.add(id(focus))


class _BudgetPort:
    def __init__(self, real):
        from wender.act.budget_governor import BudgetGovernor
        if not isinstance(real, BudgetGovernor):
            raise MetricsRefused("real BudgetGovernor required")
        self.real = real
        self._by_action = {}
        self._committed = set()

    def reserve(self, handle, cost, *, action_id, deadline_ns):
        token = self.real.reserve(handle, cost, action_id=action_id,
                                  deadline_ns=deadline_ns)
        self._by_action[action_id] = token
        return token

    def commit(self, action_id):
        reservation = self._by_action[action_id]
        self.real.commit(reservation)
        self._committed.add(id(reservation))

    def settle(self, reservation, *, consumed):
        if id(reservation) in self._committed:
            if not consumed:
                raise MetricsRefused("committed send cannot be refunded")
            self._committed.remove(id(reservation))
            return  # Real ledger is already irrevocably charged.
        self.real.settle(reservation, consumed=consumed)


def real_scheduler_probe(*, registry, locks, focus, budget, chain, observation,
                         scheduler_clock, transport_send, service_cap_ns=1_000_000):
    """Run one real WindowScheduler dispatch via strict provider port adapters.

    A genuine detector/reducer ``chain.is_settled`` must be supplied by caller:
    this adapter MUST NOT infer settlement from static RGB or fabricate a
    positive detector. Only a fake *external transport* is permissible offline.
    The transport returns a positive acknowledged terminal string or explicit
    UNKNOWN. Its callback executes under the real shared WriterFence.
    """
    from wender.act.window_scheduler import (WindowScheduler, WriterFence,
        ActionRequest, Completion, Ticket)
    from wender.act.budget_governor import BudgetGovernor
    from wender.act.focus_policy import FocusPolicy
    from wender.act.target_lock_registry import TargetLockRegistry
    from wender.perceive.session_registry import SessionRegistry
    if (not isinstance(registry, SessionRegistry)
            or not isinstance(locks, TargetLockRegistry)
            or not isinstance(focus, FocusPolicy)
            or not isinstance(budget, BudgetGovernor)):
        raise MetricsRefused("real production registry/lock/focus/budget required")
    if not callable(getattr(chain, "is_settled", None)):
        raise MetricsRefused("AUSENTE: actual detector settlement provider")
    if not callable(scheduler_clock) or not callable(transport_send):
        raise MetricsRefused("clock and external transport callback required")
    _int(service_cap_ns, "service_cap_ns", 1)
    real_record = registry.resolve(observation.handle)
    if (real_record.closed or observation.epoch != real_record.epoch
            or observation.frame.handle != observation.handle
            or observation.frame.frame_seq != observation.frame_seq):
        raise MetricsRefused("stale observation for real scheduler probe")
    reg_port = _RegistryPort(registry)
    # Existing lock/focus must have been constructed against this very port,
    # not against a fake registry or an unrelated lock provider.
    if getattr(locks, "registry", None) is not getattr(focus, "registry", None):
        raise MetricsRefused("lock/focus registry identity differs")
    if getattr(focus, "locks", None) is not locks:
        raise MetricsRefused("focus does not use the real actor lock authority")
    if not isinstance(locks.registry, _RegistryPort) or locks.registry._real is not registry:
        raise MetricsRefused("real providers need a shared _RegistryPort")
    reg_port = locks.registry
    lock_port = _LockPort(locks)
    focus_port = _FocusPort(focus, domain="p1_metrics:"+observation.handle.canonical_id)
    budget_port = _BudgetPort(budget)
    fence = WriterFence(clock=scheduler_clock)
    gates = dict.fromkeys(("identity_streams", "ownership_perception",
                          "readiness", "writer_fence"), True)

    class OfflineDispatch:
        offline = True
        prerequisite_gates = gates
        writer_fence = fence
        sends = 0

        def __call__(self, request, context):
            def effect():
                focus_port.consume(context.focus_lease, request.payload["observation"])
                budget_port.commit(request.action_id)
                lock_port.mark_inflight(context.actor_lease)
                self.sends += 1
                return transport_send(request)
            status = context.send(effect)
            if status not in ("SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN"):
                raise MetricsRefused("transport returned invalid terminal")
            positive = status != "UNKNOWN"
            if positive:
                lock_port.acknowledge(context.actor_lease)
            return Completion(request.action_id, request.handle, status,
                              acknowledged=positive)

    dispatch = OfflineDispatch()
    now = scheduler_clock()
    _int(now, "scheduler injected now")
    deadline_ns = now + max(100, 12 * service_cap_ns)
    focus.set_focus(observation.handle, domain=focus_port.domain,
                    epoch=observation.epoch)
    scheduler = WindowScheduler(registry=reg_port, locks=lock_port,
        focus_policy=focus_port, budget=budget_port, chain=chain,
        writer_fence=fence, dispatch=dispatch, clock=scheduler_clock,
        max_queue=4, max_targets=4, service_cap_ns=service_cap_ns)
    request = ActionRequest(observation.handle, "p1-metrics-send", observation.epoch,
                            {"observation": observation}, 1, deadline_ns)
    admitted = scheduler.submit(request)
    if not isinstance(admitted, Ticket):
        raise MetricsRefused("real scheduler admission refused: " + str(admitted))
    receipts = scheduler.step(scheduler_clock())
    if not receipts:
        raise MetricsRefused("AUSENTE: no terminal scheduler receipt")
    receipt = receipts[-1]
    if (receipt.action_id != request.action_id or receipt.handle != request.handle
            or receipt.status not in ("SUCCEEDED", "FAILED", "CANCELLED")):
        raise MetricsRefused("real scheduler did not return acknowledged terminal")
    if dispatch.sends != 1 or budget.spent_global != 1 or budget.pending_count != 0:
        raise MetricsRefused("send/budget accounting did not match one real dispatch")
    key = handle_identity(observation.handle)
    return {"receipt": receipt.status, "sends": dispatch.sends,
            "budget_spent": budget.spent_global, "budget_pending": budget.pending_count,
            "eligible_visits": scheduler.fair_visits.get(key, 0),
            "scheduler_diagnostics": tuple(scheduler.diagnostics),
            "scope": "VM/offline", "host_qualification": "AUSENTE"}


def make_real_scheduler_providers(*, monotonic_clock, global_budget=4,
                                  per_target_budget=4, service_cap_ns=1_000_000):
    """Construct concrete producers through one transparent registry translation."""
    from wender.perceive.session_registry import SessionRegistry
    from wender.act.target_lock_registry import TargetLockRegistry
    from wender.act.focus_policy import FocusPolicy
    from wender.act.budget_governor import BudgetGovernor
    if not callable(monotonic_clock):
        raise MetricsRefused("monotonic clock required")
    registry = SessionRegistry(clock=monotonic_clock)
    port = _RegistryPort(registry)
    locks = TargetLockRegistry(port, clock=monotonic_clock)
    focus = FocusPolicy(port, locks, clock=monotonic_clock)
    telemetry = []
    budget = BudgetGovernor(global_budget, per_target_budget,
                            service_cap_ns=service_cap_ns,
                            clock=monotonic_clock, telemetry=telemetry)
    return registry, locks, focus, budget, telemetry
