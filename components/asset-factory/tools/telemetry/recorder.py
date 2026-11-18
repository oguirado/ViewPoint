"""Buffered, process-local capture of Toro-compatible factory telemetry."""

from collections import defaultdict
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
from functools import lru_cache
from operator import itemgetter
import json
import os
from pathlib import Path
import re
import resource
import threading
import time


CONTRACT_ROOT = Path(__file__).resolve().parents[2] / "contracts/s_toro"
SCHEMA_PATH = CONTRACT_ROOT / "schemas/telemetry.schema.json"
PIN_PATH = CONTRACT_ROOT / "pins.json"
SCHEMA_ID = "af.telemetry/v1"
SESSION_PATTERN = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
BUILD_PATTERN = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
PHASES = frozenset(("end_to_end", "queue", "startup", "execution",
                    "verification", "teardown"))
ENDPOINTS = (None, "cpu", "lane", "gpu", "file", "ipc")
HASHES = ("absent", "sha256_ok", "sha256_mismatch")
PSI_FIELDS = ("cpu_some_ppm", "memory_some_ppm", "io_some_ppm")
MAX_U64 = (1 << 64) - 1
IO_COUNTERS = (
    "input_bytes", "output_bytes", "copied_bytes", "cache_hits", "cache_misses",
    "retries", "materialized_bytes", "mapped_bytes", "useful_bytes",
)
ATTRIBUTION = {
    "cpu": "CLOCK_PROCESS_CPUTIME_ID: capture producer, all threads",
    "rss": "getrusage RUSAGE_SELF ru_maxrss: capture producer lifetime high-water mark",
    "gpu": "caller-supplied peak for the declared span",
    "io": "caller-supplied counts at the declared boundary",
    "psi": "/proc/pressure: host cumulative some totals",
}
_PROCESS_TOKEN = object()
_JSON = json.JSONEncoder(separators=(",", ":"), allow_nan=False).encode
_EVENT_KEYS = (
    "timestamp_ns", "thread", "sequence", "operation", "status", "logical_bytes",
    "copy_bytes", "dram_bytes", "nvme_bytes", "wait_ns", "source", "destination",
    "hash", "lane", "generation", "reference",
)
_ENDPOINT_JSON = {value: _JSON(value) for value in ENDPOINTS}
_HASH_JSON = {value: _JSON(value) for value in HASHES}


def _after_fork():
    global _PROCESS_TOKEN
    _PROCESS_TOKEN = object()


os.register_at_fork(after_in_child=_after_fork)


@lru_cache(maxsize=1)
def _operations():
    schema = json.loads(SCHEMA_PATH.read_bytes())
    return {operation: _JSON(operation)
            for operation in schema["oneOf"][0]["properties"]["operation"]["enum"]}


def _valid_u64(value, minimum=0):
    return type(value) is int and minimum <= value <= MAX_U64


def _nullable_integer(value):
    return "null" if value is None else str(value)


def _has_text(value):
    return isinstance(value, str) and bool(value.strip())


def _counter(value, reason="uninstrumented"):
    return {"value": value, "absence_reason": reason if value is None else None}


def placement():
    """Read the calling Linux task's effective placement."""
    return {"nice": os.getpriority(os.PRIO_PROCESS, 0),
            "cpus": sorted(os.sched_getaffinity(0))}


def _cpuset(cpus):
    return ",".join(map(str, cpus))


@dataclass(frozen=True)
class PressureSample:
    timestamp_ns: int
    some_us: tuple[int | None, int | None, int | None]
    read_start_ns: int | None = None


def read_pressure() -> PressureSample:
    """Read cumulative host PSI some counters, preserving unavailable fields."""
    started = time.monotonic_ns()
    totals = []
    for name in ("cpu", "memory", "io"):
        try:
            with open(f"/proc/pressure/{name}", encoding="ascii") as source:
                fields = source.readline().split()
            value = int(fields[-1].removeprefix("total="))
            if (fields[0] != "some" or not fields[-1].startswith("total=")
                    or not _valid_u64(value)):
                raise ValueError("invalid_psi_sample")
        except (OSError, ValueError, IndexError):
            value = None
        totals.append(value)
    return PressureSample(time.monotonic_ns(), tuple(totals), started)


def pressure_ppm(samples):
    """Derive integer ppm from a timestamped pair of cumulative PSI samples."""
    if samples is None:
        return {key: _counter(None, "pressure_window_unavailable") for key in PSI_FIELDS}
    before, after = samples
    elapsed = after.timestamp_ns - before.timestamp_ns
    read_start = before.timestamp_ns if before.read_start_ns is None else before.read_start_ns
    # Each counter can be read anywhere in its sampling aperture. The earliest
    # first read and latest second read bound elapsed work. Integer microsecond
    # quantization can add at most 999 ns to the differenced counter.
    max_delta_ns = after.timestamp_ns - read_start + 999
    result = {}
    for key, start, end in zip(PSI_FIELDS, before.some_us, after.some_us, strict=True):
        if start is None or end is None:
            result[key] = _counter(None, "pressure_counter_unavailable")
            continue
        delta_ns = (end - start) * 1000
        if elapsed <= 0 or delta_ns < 0 or delta_ns > max_delta_ns:
            result[key] = _counter(None, "pressure_window_invalid")
            continue
        result[key] = _counter(min(1_000_000, delta_ns * 1_000_000 // elapsed))
    return result


class _DisabledSpan(AbstractContextManager):
    def __enter__(self):
        return self

    def __exit__(self, *_error):
        return False

    def observe_work(self):
        pass

    def account(self, **_counters):
        pass

    def set_status(self, _status):
        pass


_DISABLED_SPAN = _DisabledSpan()


class Recorder(AbstractContextManager):
    """A single writer per new JSONL file, with independent thread sequences."""

    def __init__(self, stream=None, *, session_id=None, git_sha=None,
                 toro_session=None, batch_size=256):
        self._stream = stream
        self._closed = False
        if stream is not None:
            self._process_token = _PROCESS_TOKEN
            self._header = {
                "schema": SCHEMA_ID, "session_id": session_id, "git_sha": git_sha,
                "process_id": os.getpid(),
            }
            self._cpuset = _cpuset(os.sched_getaffinity(0))
            self._event_header = _JSON({
                **self._header, "kind": "event", "toro_session": toro_session,
                "component": "fabrica",
            })[:-1]
            self._event_prefix = self._prefix(self._cpuset)
            self._batch_size = batch_size
            self._local = threading.local()
            self._buffers = []
            self._sequences = defaultdict(int)
            self._lock = threading.Lock()
            self._written = 0
            self._invalid = 0
            self._failed = False

    @classmethod
    def from_env(cls, *, git_sha=None, toro_session=None, batch_size=256,
                 env: Mapping[str, str] | None = None):
        config = os.environ if env is None else env
        if config.get("TORO_TELEMETRIA") != "1":
            return cls()
        destination = config.get("TORO_TELEMETRIA_JSONL", "")
        session_id = config.get("TORO_TELEMETRIA_SESSION_ID", "")
        if not destination.strip() or not SESSION_PATTERN.fullmatch(session_id):
            raise ValueError("capture_path_or_session")
        if git_sha is not None and not (
            isinstance(git_sha, str) and BUILD_PATTERN.fullmatch(git_sha)
        ):
            raise ValueError("invalid_build")
        if not _valid_u64(toro_session, 1) or not _valid_u64(batch_size, 1):
            raise ValueError("invalid_toro_session_or_batch_size")
        _operations()
        stream = open(destination, "xb")
        try:
            return cls(stream, session_id=session_id, git_sha=git_sha,
                       toro_session=toro_session, batch_size=batch_size)
        except BaseException:
            stream.close()
            raise

    @property
    def enabled(self):
        return self._stream is not None

    def __enter__(self):
        return self

    def __exit__(self, *_error):
        self.close()

    def _check_process(self):
        if self._process_token is not _PROCESS_TOKEN:
            raise RuntimeError("recorder_inherited_across_fork")

    def _check_open(self):
        if self._closed or self._failed:
            raise RuntimeError("capture_not_writable")

    def event(self, operation, *, status=0, logical_bytes=None, copy_bytes=None,
              dram_bytes=None, nvme_bytes=None, wait_ns=None, source=None,
              destination=None, hash_state="absent", lane=None, generation=None,
              reference=None):
        """Buffer a structural event. Validation happens when its batch drains."""
        if self.enabled:
            self._emit(operation, status, logical_bytes, copy_bytes, dram_bytes,
                       nvme_bytes, wait_ns, source, destination, hash_state,
                       lane, generation, reference)

    def _emit(self, operation, status, logical_bytes, copy_bytes, dram_bytes,
              nvme_bytes, wait_ns, source, destination, hash_state, lane,
              generation, reference, extension=None):
        self._check_process()
        buffer = getattr(self._local, "buffer", None)
        if buffer is None:
            with self._lock:
                self._check_open()
                buffer = (threading.get_native_id(), threading.Lock(), [])
                self._buffers.append(buffer)
                self._local.buffer = buffer
        thread, lock, pending = buffer
        with lock:
            self._check_open()
            pending.append((time.monotonic_ns(), thread, operation, status,
                            logical_bytes, copy_bytes, dram_bytes, nvme_bytes, wait_ns,
                            source, destination, hash_state, lane, generation, reference,
                            extension))
            full = len(pending) >= self._batch_size
        if full:
            with self._lock:
                if not self._closed:
                    self._check_open()
                    if len(pending) >= self._batch_size:
                        self._flush_batch()

    def _prefix(self, cpuset):
        return self._event_header + ',"cpuset":' + _JSON(cpuset) + ','

    def _format_event(self, event):
        timestamp, thread, operation, status, logical_bytes, copy_bytes, dram_bytes, \
            nvme_bytes, wait_ns, source, destination, hash_state, lane, generation, \
            reference, extension = event
        if (
            not isinstance(operation, str) or operation not in _operations()
            or type(status) is not int or not -(1 << 31) <= status < (1 << 31)
            or any(value is not None and not _valid_u64(value) for value in
                   (logical_bytes, copy_bytes, dram_bytes, nvme_bytes, wait_ns))
            or any(value is not None and not _valid_u64(value, 1)
                   for value in (lane, generation, reference))
            or source not in ENDPOINTS or destination not in ENDPOINTS
            or (copy_bytes and (source is None or destination is None))
            or hash_state not in HASHES
        ):
            self._invalid += 1
            return None
        self._sequences[thread] += 1
        sequence = self._sequences[thread]
        if extension is not None:
            row = dict(zip(_EVENT_KEYS, (timestamp, thread, sequence, *event[2:-1])))
            row["extension"] = extension
            prefix = self._prefix(_cpuset(extension["placement_work"]["cpus"]))
            return prefix + _JSON(row)[1:] + "\n"
        integer = _nullable_integer
        return (
            f'{self._event_prefix}"timestamp_ns":{timestamp},"thread":{thread},"sequence":{sequence},'
            f'"operation":{_operations()[operation]},"status":{status},'
            f'"logical_bytes":{integer(logical_bytes)},"copy_bytes":{integer(copy_bytes)},'
            f'"dram_bytes":{integer(dram_bytes)},"nvme_bytes":{integer(nvme_bytes)},'
            f'"wait_ns":{integer(wait_ns)},"source":{_ENDPOINT_JSON[source]},'
            f'"destination":{_ENDPOINT_JSON[destination]},"hash":{_HASH_JSON[hash_state]},'
            f'"lane":{integer(lane)},"generation":{integer(generation)},'
            f'"reference":{integer(reference)}' + "}\n"
        )

    def _write(self, output):
        if self._stream.write(output) != len(output):
            raise OSError("short_telemetry_write")
        self._stream.flush()

    def _drain(self):
        # Keep all producer locks through the snapshot so a later flush cannot
        # publish a timestamp older than a row in this batch.
        with ExitStack() as stack:
            for _, lock, _ in self._buffers:
                stack.enter_context(lock)
            events = []
            for _, _, pending in self._buffers:
                events.extend(pending)
                pending.clear()
        return sorted(events, key=itemgetter(0))

    def _flush(self):
        invalid_before = self._invalid
        try:
            rows = []
            for event in self._drain():
                row = self._format_event(event)
                if row is not None:
                    rows.append(row)
            if rows:
                self._write("".join(rows).encode("utf-8"))
                self._written += len(rows)
        except (OSError, TypeError, ValueError):
            self._failed = True
            raise
        return self._invalid != invalid_before

    def _flush_batch(self):
        if self._flush():
            raise ValueError("invalid_event")

    def flush(self):
        """Drain buffered complete events without ending the capture."""
        if not self.enabled:
            return
        self._check_process()
        with self._lock:
            self._check_open()
            self._flush_batch()

    def close(self):
        """Flush and record the final count. Failed I/O leaves an incomplete file."""
        if not self.enabled:
            return
        self._check_process()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                if not self._failed:
                    invalid = self._flush()
                    checkpoint = {
                        **self._header, "kind": "checkpoint", "cpuset": self._cpuset,
                        "timestamp_ns": time.monotonic_ns(),
                        "final": True, "record_count": self._written, "ring_drops": 0,
                        "unregistered_drops": 0, "invalid_drops": self._invalid,
                    }
                    self._write((_JSON(checkpoint) + "\n").encode("utf-8"))
                    if invalid:
                        raise ValueError("invalid_event")
            finally:
                self._stream.close()

    def span(self, operation, *, stream_id, span_id, request_id, contract_pins_sha256,
             arm_id, pair_id, variant, phase, clock_id, boundary_id, stage=None,
             parent_span_id=None, worker=None, worker_generation=None, test_id=None,
             pressure_pre=None, pressure_sink: Callable | None = None, lane=None,
             generation=None, reference=None):
        """Capture one closed span. Call observe_work on the actual producing task."""
        if not self.enabled:
            return _DISABLED_SPAN
        identity = {
            "stream_id": stream_id, "span_id": span_id, "request_id": request_id,
            "contract_pins_sha256": contract_pins_sha256, "arm_id": arm_id,
            "pair_id": pair_id, "variant": variant, "phase": phase,
            "clock_id": clock_id, "boundary_id": boundary_id,
            "stage": operation if stage is None else stage,
            "parent_span_id": parent_span_id, "worker": worker,
            "worker_generation": worker_generation, "test_id": test_id,
        }
        return _Span(self, operation, identity, pressure_pre, pressure_sink,
                     lane, generation, reference)


class _Span(AbstractContextManager):
    def __init__(self, recorder, operation, identity, pressure_pre, pressure_sink,
                 lane, generation, reference):
        optional = ("parent_span_id", "worker", "worker_generation", "test_id")
        if (any(not _has_text(value) for key, value in identity.items() if key not in optional)
                or not DIGEST_PATTERN.fullmatch(identity["contract_pins_sha256"])
                or identity["variant"] not in ("A", "B") or identity["phase"] not in PHASES
                or any(identity[key] is not None and not _has_text(identity[key])
                       for key in ("parent_span_id", "worker", "test_id"))
                or (identity["worker_generation"] is not None
                    and not _valid_u64(identity["worker_generation"], 1))):
            raise ValueError("invalid_span_identity")
        self._recorder = recorder
        self._operation = operation
        self._identity = identity
        self._pre = pressure_pre
        self._pressure_sink = pressure_sink
        self._lane = lane
        self._generation = generation
        self._reference = reference
        self._counters = {}
        self._status = 0

    def __enter__(self):
        self._thread = threading.get_native_id()
        self._entry = placement()
        self._work = None
        self._before = read_pressure()
        self._cpu_start = time.process_time_ns()
        self._start = time.monotonic_ns()
        return self

    def observe_work(self):
        if self._thread != threading.get_native_id():
            raise RuntimeError("span_thread_changed")
        self._work = placement()

    def account(self, **counters):
        """Supply final counts actually measured by this boundary's owner."""
        numeric = set(IO_COUNTERS) | {
            "peak_gpu_bytes", "copy_bytes", "logical_bytes", "dram_bytes", "nvme_bytes", "wait_ns",
        }
        if any(
            (key not in ("source", "destination") or value not in ENDPOINTS)
            if key not in numeric else (value is not None and not _valid_u64(value))
            for key, value in counters.items()
        ) or "copied_bytes" in counters:
            raise ValueError("invalid_span_counters")
        self._counters.update(counters)

    def set_status(self, status):
        self._status = status

    def __exit__(self, error_type, _error, _traceback):
        end = time.monotonic_ns()
        cpu_ns = time.process_time_ns() - self._cpu_start
        if self._work is None or self._thread != threading.get_native_id():
            if error_type is not None:
                return False
            raise RuntimeError("work_placement_unavailable")
        raw_pressure = (self._before, read_pressure())
        values = self._counters
        extension = {
            **self._identity,
            **{key: _counter(values.get(key)) for key in IO_COUNTERS},
            "dropped": 0, "start_ns": self._start, "end_ns": end,
            "placement_entry": self._entry, "placement_work": self._work,
            "attribution": dict(ATTRIBUTION),
            "cpu_ns": _counter(cpu_ns),
            "peak_rss_bytes": _counter(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024),
            "peak_gpu_bytes": _counter(values.get("peak_gpu_bytes"), "gpu_peak_not_visible"),
            "copied_bytes": _counter(values.get("copy_bytes")),
            "psi_pre": pressure_ppm(self._pre), "psi": pressure_ppm(raw_pressure),
        }
        if self._pressure_sink is not None:
            self._pressure_sink(self._identity["span_id"], self._pre, raw_pressure)
        self._recorder._emit(
            self._operation, self._status if error_type is None else 2,
            values.get("logical_bytes"), values.get("copy_bytes"), values.get("dram_bytes"),
            values.get("nvme_bytes"), values.get("wait_ns"),
            values.get("source"), values.get("destination"), "absent",
            self._lane, self._generation, self._reference, extension,
        )
        return False
