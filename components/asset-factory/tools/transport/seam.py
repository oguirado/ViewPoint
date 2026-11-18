"""Select the artifact backend and emit boundary copy accounting."""
from collections import Counter
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from threading import Lock, get_native_id
from time import monotonic_ns
from uuid import UUID, uuid4

from .artifact_ref import ArtifactError, ArtifactRef, TransferAccounting


_PINS = Path(__file__).resolve().parents[2] / "contracts/s_toro/pins.json"


def _placement():
    return {"nice": os.getpriority(os.PRIO_PROCESS, 0), "cpus": sorted(os.sched_getaffinity(0))}


def _counter(value, absence_reason="boundary_counter_unavailable"):
    return {"value": value, "absence_reason": absence_reason if value is None else None}


@dataclass(frozen=True)
class TransportEvent:
    operation: str
    status: int
    logical_bytes: int | None
    copy_bytes: int | None
    materialized_bytes: int | None
    mapped_bytes: int | None
    verified_bytes: int | None
    start_ns: int
    end_ns: int
    placement_entry: dict
    fallback_reason: str | None
    reason: str | None

    @property
    def elapsed_ns(self):
        return self.end_ns - self.start_ns


class JsonlTelemetry:
    """Process-local structural Toro-compatible events and a final checkpoint."""

    def __init__(self, path, session_id, *, git_sha=None):
        if not re.fullmatch(r"[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}", session_id or ""):
            raise ArtifactError(2, "telemetry_session_invalid")
        if git_sha is not None and not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", git_sha):
            raise ArtifactError(2, "telemetry_build_invalid")
        self._session = session_id
        # The wire requires a nonzero u64. The UUID remains the capture identity.
        self._toro_session = UUID(session_id).int % ((1 << 64) - 1) + 1
        self._git_sha = git_sha
        self._pins_sha256 = sha256(_PINS.read_bytes()).hexdigest()
        self._stream_id = uuid4().hex
        self._pid = os.getpid()
        self._sequence = Counter()
        self._lock = Lock()
        self._failed = False
        self._file = Path(path).open("x", encoding="utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @staticmethod
    def _write(stream, record):
        stream.write(json.dumps(record, separators=(",", ":")) + "\n")
        stream.flush()

    def _header(self, cpus):
        return dict(schema="af.telemetry/v1", session_id=self._session, git_sha=self._git_sha,
                    cpuset=",".join(map(str, cpus)), process_id=self._pid, timestamp_ns=monotonic_ns())

    def _extension(self, event, thread, sequence, work):
        span_id = f"{self._stream_id}-{thread}-{sequence}"
        unmeasured = _counter(None, "not_sampled_by_transport")
        pressure = dict.fromkeys(("cpu_some_ppm", "memory_some_ppm", "io_some_ppm"), unmeasured)
        attribution = dict.fromkeys(("cpu", "rss", "gpu", "psi"), "not_sampled_by_transport")
        attribution["io"] = "observed application payload bytes at the artifact boundary"
        extension = dict(stream_id=self._stream_id, dropped=0, span_id=span_id, parent_span_id=None,
                         stage=event.operation, worker=None, worker_generation=None, test_id=None,
                         request_id=span_id, contract_pins_sha256=self._pins_sha256,
                         arm_id="unscheduled", pair_id="unscheduled", variant="B", phase="execution",
                         start_ns=event.start_ns, end_ns=event.end_ns, clock_id="CLOCK_MONOTONIC",
                         placement_entry=event.placement_entry, placement_work=work, attribution=attribution,
                         boundary_id=event.operation, cpu_ns=unmeasured, peak_rss_bytes=unmeasured,
                         peak_gpu_bytes=unmeasured, input_bytes=_counter(event.logical_bytes),
                         output_bytes=_counter(event.verified_bytes), copied_bytes=_counter(event.copy_bytes),
                         materialized_bytes=_counter(event.materialized_bytes), mapped_bytes=_counter(event.mapped_bytes),
                         useful_bytes=_counter(event.verified_bytes), cache_hits=unmeasured,
                         cache_misses=unmeasured, retries=unmeasured, psi=pressure, psi_pre=pressure)
        if event.fallback_reason is not None:
            extension["fallback_reason"] = event.fallback_reason
        if event.reason is not None:
            extension["reason"] = event.reason[:256] or "unspecified"
        return extension

    def _check_stream(self):
        if os.getpid() != self._pid or self._file.closed or self._failed:
            raise ArtifactError(5, "telemetry_stream_unavailable")

    def __call__(self, event):
        self._check_stream()
        thread = get_native_id()
        put = event.operation == "artifact_put"
        digest_status = "absent"
        if event.status == 0:
            digest_status = "sha256_ok"
        elif event.reason == "digest_mismatch":
            digest_status = "sha256_mismatch"
        with self._lock:
            self._check_stream()
            sequence = self._sequence[thread] + 1
            try:
                work = _placement()
                row = dict(self._header(work["cpus"]), kind="event", thread=thread,
                           sequence=sequence, toro_session=self._toro_session,
                           lane=None, generation=None, reference=None, component="fabrica",
                           operation=event.operation, status=event.status,
                           logical_bytes=event.logical_bytes, copy_bytes=event.copy_bytes,
                           dram_bytes=None, nvme_bytes=None, wait_ns=None,
                           source="cpu" if put else "file", destination="file" if put else "cpu",
                           hash=digest_status, extension=self._extension(event, thread, sequence, work))
                self._write(self._file, row)
            except BaseException:
                self._failed = True
                raise
            self._sequence[thread] = sequence

    def close(self):
        if os.getpid() != self._pid:
            raise ArtifactError(5, "telemetry_stream_unavailable")
        with self._lock:
            try:
                if not self._file.closed and not self._failed:
                    row = dict(self._header(_placement()["cpus"]), kind="checkpoint", final=True,
                               record_count=self._sequence.total(), ring_drops=0,
                               unregistered_drops=0, invalid_drops=0)
                    self._write(self._file, row)
            finally:
                self._file.close()


class Transport:
    """Shared entry point for local artifact publication and consumption.

    The Toro stub reports unavailability before accepting any work. When observing,
    the sink receives one accounting event per completed boundary.
    """

    def __init__(self, normal, *, mode=None, emit=None, git_sha=None):
        self.mode = os.environ.get("FACTORY_TORO", "off") if mode is None else mode
        if self.mode not in ("off", "observe", "on"):
            raise ArtifactError(2, "mode_invalid")
        self._backend = self._toro_backend() if self.mode == "on" else None
        self.backend = "normal" if self._backend is None else "toro"
        self.fallback_reason = (
            "toro_unavailable" if self.mode == "on" and self._backend is None else None)
        if self._backend is None:
            self._backend = normal
        self._telemetry = None
        self._emit = None if self.mode == "off" else emit
        if self.mode != "off" and emit is None and os.environ.get("TORO_TELEMETRIA") == "1":
            path = os.environ.get("TORO_TELEMETRIA_JSONL")
            if not path:
                raise ArtifactError(2, "telemetry_path_missing")
            self._telemetry = JsonlTelemetry(
                path, os.environ.get("TORO_TELEMETRIA_SESSION_ID"), git_sha=git_sha)
            self._emit = self._telemetry
        self.latest_event = None

    @staticmethod
    def _toro_backend():
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self._telemetry is not None:
            self._telemetry.close()

    @contextmanager
    def _measure(self, operation):
        placement = _placement()
        start = monotonic_ns()
        accounting = TransferAccounting()
        try:
            yield accounting
        except ArtifactError as exc:
            self._record(operation, start, placement, accounting, exc.rc, exc.reason)
            raise
        except BaseException as exc:
            self._record(operation, start, placement, accounting, 5, type(exc).__name__)
            raise
        else:
            self._record(operation, start, placement, accounting, 0, None)

    def _record(self, operation, start, placement, accounting, status, reason):
        end = monotonic_ns()
        event = TransportEvent(operation, status, accounting.logical_bytes, accounting.copy_bytes,
                               accounting.materialized_bytes, accounting.mapped_bytes,
                               accounting.verified_bytes, start, end, placement,
                               self.fallback_reason, reason)
        self.latest_event = event
        if self._emit is not None:
            self._emit(event)

    def put(self, payload, **options) -> ArtifactRef:
        if self.mode == "off":
            return self._backend.put(payload, **options)
        with self._measure("artifact_put") as accounting:
            return self._backend.put(payload, accounting=accounting, **options)

    @contextmanager
    def open(self, ref, access):
        if self.mode == "off":
            with self._backend.open(ref, access) as view:
                yield view
            return
        with ExitStack() as custody:
            with self._measure("artifact_open") as accounting:
                view = custody.enter_context(self._backend.open(ref, access, accounting=accounting))
            yield view

    def release(self, ref, access):
        self._backend.release(ref, access)
