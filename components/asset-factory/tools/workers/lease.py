"""Single-resident supervisor implementing af.worker-lease/v1.

Construct this in the trusted provider controller, with an admitted worker
program and independent scheduler/rights callbacks. The launch permit consumer
must atomically reserve launch ownership and consume the bound single-use permit
in the existing authority. The retirement callback verifies and releases that
reservation. Both ledgers must survive a controller restart and refuse admission
while previous ownership is unproven.

The private local mailbox is an adapter contract. The program echoes launch.json
as ready.json after initialization. For each request.json it consumes the request,
then atomically writes reply.json with the same identity fields and a value in
place of payload. A qualified adapter also returns post_job_state after its reset
hooks run. Work results are provisional until the supervisor returns them.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from subprocess import TimeoutExpired
from tempfile import TemporaryDirectory
from threading import Lock
import time
from uuid import uuid4

from asset_factory import core, proc

_MAX_MESSAGE_BYTES = 64 * 1024
_NS = 1_000_000_000
_POLL_NS = 50_000_000
_MAX_CONSUMED_PERMITS = 4096
_MAX_PERMIT_LENGTH = 256


class LeaseError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Ceiling:
    memory_high_bytes: int
    memory_max_bytes: int
    swap_max_bytes: int
    cpus: tuple[int, ...]

    def verify(self):
        try:
            declared = proc.MemoryLimits(self.memory_high_bytes, self.memory_max_bytes,
                                         self.swap_max_bytes)
            observed = proc._inherited_limits()
        except proc.SignalRefusal as exc:
            raise LeaseError("resource_ceiling_unproven") from exc
        if (declared != observed or type(self.cpus) is not tuple
                or any(type(cpu) is not int for cpu in self.cpus)
                or self.cpus != tuple(sorted(os.sched_getaffinity(0)))
                or not set(self.cpus) <= set(range(16))):
            raise LeaseError("resource_ceiling_unproven")
        return declared


@dataclass(frozen=True)
class AdapterScope:
    adapter_id: str
    adapter_sha256: str
    workload_kind: str
    probes: tuple[str, ...]


@dataclass(frozen=True)
class ReuseQualification:
    receipt_sha256: str
    receipt: bytes


def _probe_state(state, probes, expected):
    return (type(state) is dict and bool(probes) and state.keys() == set(probes)
            and all(state[probe] is expected for probe in probes))


def _isolation_identity(value):
    return (type(value) is list and len(value) == 2
            and all(type(part) is int and part > 0 for part in value)
            and value[0] > 1)


def verify_isolation(scope, qualification):
    """Verify the receipt minted by the trusted authority against the installed adapter scope."""
    if (type(scope) is not AdapterScope or type(qualification) is not ReuseQualification
            or type(qualification.receipt) is not bytes
            or len(qualification.receipt) > _MAX_MESSAGE_BYTES
            or sha256(qualification.receipt).hexdigest() != qualification.receipt_sha256):
        raise LeaseError("worker_isolation_receipt")
    try:
        evidence = json.loads(qualification.receipt)
        canonical = core.canonical_json(evidence).encode()
    except (core.ContractError, UnicodeError, TypeError, ValueError, RecursionError) as exc:
        raise LeaseError("worker_isolation_receipt") from exc
    if (type(evidence) is not dict or canonical != qualification.receipt
            or evidence.get("schema") != "af.worker-isolation/v1"):
        raise LeaseError("worker_isolation_receipt")
    if (evidence.get("adapter_id") != scope.adapter_id
            or evidence.get("adapter_sha256") != scope.adapter_sha256
            or evidence.get("workload_kind") != scope.workload_kind):
        raise LeaseError("worker_isolation_scope")
    first, second = evidence.get("identity_a"), evidence.get("identity_b")
    if (not _isolation_identity(first) or not _isolation_identity(second) or first != second
            or not _probe_state(evidence.get("perturbed"), scope.probes, False)
            or not all(_probe_state(evidence.get(stage), scope.probes, True)
                       for stage in ("before", "post_a", "job_b", "post_b"))):
        raise LeaseError("worker_isolation_evidence")


@dataclass(frozen=True)
class Binding:
    provider_id: str
    candidate_sha256: str
    provider_sha256: str
    runtime_sha256: str
    admission_sha256: str
    ceiling: Ceiling
    reuse: ReuseQualification | None = None


@dataclass(frozen=True)
class Lease:
    owner: str
    clock_id: str
    generation: int
    lease_epoch: int
    lease_id: str
    issued_ns: int
    expires_ns: int
    binding: Binding
    state: str = "active"
    closed_ns: int | None = None
    close_reason: str | None = None

    def as_record(self):
        record = asdict(self)
        binding = record.pop("binding")
        # The authority decision stays in the in-memory binding. The v1 wire
        # record retains its closed schema and existing admission digest.
        binding.pop("reuse")
        ceiling = binding.pop("ceiling")
        ceiling["cpus"] = list(ceiling["cpus"])
        return {"schema": "af.worker-lease/v1", **record, **binding, "resource_ceiling": ceiling}


def _remaining(deadline_ns):
    left = deadline_ns - time.monotonic_ns()
    if left <= 0:
        raise LeaseError("request_deadline")
    return left / _NS


def _read_message(path):
    # A pipe, directory, symlink or device cannot turn a bounded wait into a
    # blocking read. The descriptor pins the regular inode through parsing.
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise LeaseError("worker_protocol")
        return core.load_json(Path(f"/proc/self/fd/{descriptor}"), max_bytes=_MAX_MESSAGE_BYTES)
    finally:
        os.close(descriptor)


class Residency:
    """Own one job and serialize all custody and process-helper operations.

    authorize(operation, owner, payload) returns the current trusted Binding or
    refuses. A stable admission digest identifies the reservation, even when
    rechecking it for successive requests. A query must also admit its payload.
    The scheduler mints permits for launch_target() and atomically consumes
    them through consume_permit(permit, binding, clock_id, generation,
    lease_epoch). The clock ID identifies this worker controller. Retirement
    is witnessed by confirm_retired(binding, clock_id, generation, unit_name).
    Both callbacks must return True only on a proven transition.

    The trusted installer identifies the program and workload via adapter_scope.
    Only authorize() can grant reuse, with an exact matching isolation receipt.
    Other adapters recycle after work.
    """

    def __init__(self, owner, program, *, authorize, consume_permit, confirm_retired,
                 runtime_s, cwd, env, adapter_scope=None):
        if type(owner) is not str or not owner.strip():
            raise LeaseError("invalid_owner")
        if type(runtime_s) is not int or not 1 <= runtime_s <= 86400:
            raise LeaseError("invalid_runtime")
        self._owner = owner
        self._creator = os.getpid()
        self._clock_id = uuid4().hex
        self._program = program
        self._authorize = authorize
        self._consume = consume_permit
        self._confirm_retired = confirm_retired
        self._runtime_s = runtime_s
        self._cwd = Path(cwd).resolve(strict=True)
        self._env = dict(env)
        self._lock = Lock()
        self._generation = 0
        self._launch_generation = 0
        self._epoch = 0
        self._job = None
        self._job_used = False
        self._adapter_scope = adapter_scope
        self._consumed_permits = set()
        self._unit_name = None
        self._mailbox = None
        self._binding = None
        self._active = None
        self._terminal = None
        self._fenced = False

    @property
    def last_terminal(self):
        return self._terminal

    @contextmanager
    def _transaction(self, deadline_ns):
        if os.getpid() != self._creator:
            raise LeaseError("lease_identity")
        if type(deadline_ns) is not int:
            raise LeaseError("invalid_deadline")
        if not self._lock.acquire(timeout=min(_remaining(deadline_ns), self._runtime_s)):
            raise LeaseError("request_deadline")
        try:
            _remaining(deadline_ns)
            yield
        finally:
            self._lock.release()

    def _current(self, operation, payload=None):
        binding = self._authorize(operation, self._owner, payload)
        if (type(binding) is not Binding or type(binding.provider_id) is not str
                or not binding.provider_id.strip() or type(binding.ceiling) is not Ceiling
                or any(type(pin) is not str or re.fullmatch(r"[0-9a-f]{64}", pin) is None
                       for pin in (binding.candidate_sha256, binding.provider_sha256,
                                   binding.runtime_sha256, binding.admission_sha256))):
            raise LeaseError("lease_authority")
        if self._binding is None:
            if binding.reuse is not None:
                verify_isolation(self._adapter_scope, binding.reuse)
        elif self._binding != binding:
            raise LeaseError("lease_identity")
        return binding, binding.ceiling.verify()

    def _close(self, state, reason):
        if self._active is not None:
            self._terminal = replace(self._active, state=state, closed_ns=time.monotonic_ns(),
                                     close_reason=reason)
            self._active = None
        return self._terminal

    def _drain(self):
        if self._binding is None:
            return
        try:
            if self._job is not None:
                self._job.stop()
                if self._job.result is None:
                    raise LeaseError("release_unproven")
                self._job = None
                self._job_used = False
            if self._mailbox is not None:
                self._mailbox.cleanup()
                self._mailbox = None
            if self._confirm_retired(self._binding, self._clock_id, self._launch_generation,
                                     self._unit_name) is not True:
                raise LeaseError("release_unproven")
        except Exception as exc:
            raise LeaseError("release_unproven") from exc
        self._mailbox = self._binding = self._unit_name = None

    def _finish(self, state, reason, *, broken=False):
        self._fenced = True
        terminal = self._close(state, reason)
        try:
            self._drain()
        except LeaseError:
            if not broken:
                raise
        else:
            self._fenced = broken
        return terminal

    def _check_alive(self, deadline_ns):
        _remaining(deadline_ns)
        try:
            bound = deadline_ns / _NS
            result = proc.poll_exit(self._job, deadline=bound, retirement_deadline=bound)
        except (TimeoutExpired, proc.SignalRefusal) as exc:
            _remaining(deadline_ns)
            raise LeaseError("worker_status_unproven") from exc
        if result is not None:
            raise LeaseError("worker_crashed")
        _remaining(deadline_ns)

    def _await(self, name, deadline_ns):
        path = Path(self._mailbox.name) / name
        next_poll = time.monotonic_ns() + _POLL_NS
        while True:
            _remaining(deadline_ns)
            try:
                value = _read_message(path)
            except FileNotFoundError:
                pass
            else:
                _remaining(deadline_ns)
                return value
            if time.monotonic_ns() >= next_poll:
                self._check_alive(deadline_ns)
                next_poll = time.monotonic_ns() + _POLL_NS
            time.sleep(min(.003, _remaining(deadline_ns)))

    @staticmethod
    def _matches(claim, record):
        if type(claim) is not Lease or record is None:
            return False
        try:
            return (claim.binding.reuse == record.binding.reuse
                    and core.canonical_json(claim.as_record()) == core.canonical_json(record.as_record()))
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    def _verify_live(self, lease, operation, deadline_ns, payload=None):
        if self._fenced:
            raise LeaseError("generation_broken")
        if not self._matches(lease, self._active):
            if (self._active is None and self._terminal is not None
                    and self._matches(lease, replace(self._terminal, state="active", closed_ns=None,
                                                     close_reason=None))):
                raise LeaseError("lease_already_closed")
            raise LeaseError("lease_identity")
        if time.monotonic_ns() >= lease.expires_ns:
            self._finish("invalidated", "lease_expired")
            raise LeaseError("lease_expired")
        try:
            self._current(operation, payload)
        except Exception as exc:
            self._finish("invalidated", getattr(exc, "code", "lease_authority"))
            raise
        if time.monotonic_ns() >= lease.expires_ns:
            self._finish("invalidated", "lease_expired")
            raise LeaseError("lease_expired")
        try:
            self._check_alive(min(deadline_ns, lease.expires_ns))
        except LeaseError as exc:
            if exc.code == "request_deadline" and time.monotonic_ns() >= lease.expires_ns:
                self._finish("invalidated", "lease_expired")
                raise LeaseError("lease_expired") from exc
            if exc.code != "request_deadline":
                self._finish("crashed", exc.code, broken=True)
            raise

    def _next_launch_target(self):
        return self._clock_id, self._launch_generation + 1, 1

    def launch_target(self, *, deadline_ns):
        """Return the prospective cold-launch identity for independent permit minting."""
        with self._transaction(deadline_ns):
            return self._next_launch_target()

    def load(self, *, expires_ns, deadline_ns, permit=None):
        """Borrow the current generation or launch a newly authorized one."""
        with self._transaction(deadline_ns):
            if self._fenced:
                raise LeaseError("generation_broken")
            if type(expires_ns) is not int or expires_ns <= time.monotonic_ns():
                raise LeaseError("lease_expired")
            if self._active is not None:
                if time.monotonic_ns() < self._active.expires_ns:
                    raise LeaseError("lease_busy")
                self._finish("invalidated", "lease_expired")
                raise LeaseError("lease_expired")
            deadline_ns = min(deadline_ns, expires_ns)
            try:
                binding, limits = self._current("load")
            except Exception as exc:
                if self._binding is not None:
                    self._finish("invalidated", getattr(exc, "code", "lease_authority"))
                raise
            _remaining(deadline_ns)
            if self._job is not None:
                epoch = self._epoch + 1
                if permit is not None:
                    raise LeaseError("permit_unexpected")
                try:
                    self._check_alive(deadline_ns)
                except LeaseError as exc:
                    self._finish("crashed", exc.code, broken=True)
                    raise
            else:
                epoch = 1
                self._launch(binding, limits, permit, deadline_ns)
            issued = time.monotonic_ns()
            if issued >= deadline_ns:
                self._finish("invalidated", "request_deadline", broken=True)
                raise LeaseError("request_deadline")
            lease = Lease(self._owner, self._clock_id, self._launch_generation, epoch,
                          uuid4().hex, issued, expires_ns, binding)
            self._generation, self._epoch, self._active = lease.generation, epoch, lease
            return lease

    def _launch(self, binding, limits, permit, deadline_ns):
        if permit is None:
            raise LeaseError("permit_required")
        if (type(permit) is not str or not permit or len(permit) > _MAX_PERMIT_LENGTH
                or permit in self._consumed_permits):
            raise LeaseError("permit_replayed_or_invalid")
        # Do not evict a consumed ID and silently weaken local replay protection.
        if len(self._consumed_permits) >= _MAX_CONSUMED_PERMITS:
            raise LeaseError("permit_history_full")
        # Prepare the local transport first. Nothing runs before consumption.
        mailbox = TemporaryDirectory(prefix="af-resident-")
        root = Path(mailbox.name)
        clock_id, generation, lease_epoch = self._next_launch_target()
        try:
            identity = {"clock_id": clock_id, "generation": generation,
                        "runtime_sha256": binding.runtime_sha256}
            core.atomic_write_json(root / "launch.json", identity)
            argv = self._program(root)
            _remaining(deadline_ns)
        except BaseException:
            mailbox.cleanup()
            raise
        previous_launch = self._launch_generation
        self._launch_generation = generation
        self._binding = binding
        self._mailbox = mailbox
        try:
            try:
                consumed = self._consume(permit, binding, clock_id, generation, lease_epoch)
            except BaseException:
                self._consumed_permits.add(permit)
                raise
            if consumed is not True:
                self._launch_generation = previous_launch
                self._binding = self._mailbox = None
                mailbox.cleanup()
                raise LeaseError("permit_replayed_or_invalid")
            self._consumed_permits.add(permit)
            _remaining(deadline_ns)
            self._job = proc.popen(argv, start_new_session=True, memory_limits=limits,
                                  cwd=self._cwd, env=self._env, runtime_s=self._runtime_s)
            self._unit_name = self._job.unit_name
            if core.canonical_json(self._await("ready.json", deadline_ns)) != core.canonical_json(identity):
                raise LeaseError("reply_identity")
            self._check_alive(deadline_ns)
        except BaseException as exc:
            if self._binding is not None:
                self._unit_name = self._unit_name or getattr(exc, "unit_name", None)
                self._finish("crashed", "launch_unproven", broken=True)
            raise

    def query(self, lease, payload, *, deadline_ns):
        """Authenticate a request and its acknowledgement under an active grant."""
        with self._transaction(deadline_ns):
            raw = core.canonical_json(payload)
            if len(raw.encode()) > _MAX_MESSAGE_BYTES:
                raise LeaseError("worker_message_too_large")
            payload = json.loads(raw)
            self._verify_live(lease, "query", deadline_ns, json.loads(raw))
            if self._job_used and lease.binding.reuse is None:
                self._finish("invalidated", "worker_reuse_unqualified")
                raise LeaseError("worker_reuse_unqualified")
            bound = min(deadline_ns, lease.expires_ns)
            identity = {"clock_id": lease.clock_id, "generation": lease.generation,
                        "lease_epoch": lease.lease_epoch, "lease_id": lease.lease_id,
                        "request_id": uuid4().hex, "deadline_ns": bound}
            request = {**identity, "payload": payload}
            if len(core.canonical_json(request).encode()) > _MAX_MESSAGE_BYTES:
                raise LeaseError("worker_message_too_large")
            _remaining(bound)
            root = Path(self._mailbox.name)
            request_file, reply_file = root / "request.json", root / "reply.json"
            try:
                if os.path.lexists(request_file) or os.path.lexists(reply_file):
                    raise LeaseError("worker_protocol")
                self._job_used = True
                core.atomic_write_json(request_file, request)
                reply = self._await("reply.json", bound)
                if type(reply) is not dict or "value" not in reply:
                    raise LeaseError("reply_identity")
                value = reply.pop("value")
                if lease.binding.reuse is not None:
                    state = reply.pop("post_job_state", None)
                    if not _probe_state(state, self._adapter_scope.probes, True):
                        raise LeaseError("worker_isolation_unproven")
                if (core.canonical_json(reply) != core.canonical_json(identity)
                        or os.path.lexists(request_file)):
                    raise LeaseError("reply_identity")
                reply_file.unlink()
                self._current("query", json.loads(raw))
                self._check_alive(bound)
            except BaseException as exc:
                code = getattr(exc, "code", "worker_protocol")
                self._finish("crashed" if code == "worker_crashed" else "invalidated",
                             code, broken=True)
                raise
            return value

    def release(self, lease, *, deadline_ns):
        """End custody and retain the resident only when it is safe to reuse."""
        with self._transaction(deadline_ns):
            self._verify_live(lease, "release", deadline_ns)
            if self._job_used and lease.binding.reuse is None:
                return self._retire_active(deadline_ns, "released")
            return self._close("released", "released")

    def _retire_active(self, deadline_ns, reason):
        self._fenced = True
        try:
            self._drain()
        except LeaseError:
            self._close("invalidated", "release_unproven")
            raise
        terminal = self._close("released", reason)
        self._fenced = False
        _remaining(deadline_ns)
        return terminal

    def unload(self, lease, *, deadline_ns):
        """End active custody and prove process and scheduler retirement."""
        with self._transaction(deadline_ns):
            self._verify_live(lease, "unload", deadline_ns)
            return self._retire_active(deadline_ns, "unloaded")

    def recover(self, *, deadline_ns):
        """Drain or reconcile retained ownership before allowing a fresh permit."""
        with self._transaction(deadline_ns):
            self._fenced = True
            self._close("invalidated", "recover")
            self._drain()
            _remaining(deadline_ns)
            self._fenced = False
