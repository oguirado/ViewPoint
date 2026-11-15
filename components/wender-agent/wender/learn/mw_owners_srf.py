# @module {"id":"wa.learn.mw_owners_srf","layer":4,"owner":"LEARN","deps":["wa.learn.mw_locate_ai","wa.learn.mw_workflow","wa.learn.script_live","wa.perceive.srf_identity","wa.act.actor","wa.scripts.types","wa.tools.memoryweb_gate"],"book":"HB-WA-s4-learn"}
"""Trusted identity composition for declarative workflows; no workflow plugins.

Origins: MEMORYWEB_BUILD_ARCH.md MWP7, MEMORYWEB_PROTOCOL.md execution and
custody contracts, and scripts_mesh/build/packs/bd_mwp7_mwctx.md (corrected by
mwp7_w.md). Site authority and actual clipboard cleanup remain trusted caller dependencies.
StdinPrivatePaste bypasses argv-based type via a bounded private pipe; support
is negotiated by a non-actuating parser rejection, never a dispatch probe.
Shell-facing trusted provisioning and production clipboard ownership are
AUSENTE; producer support alone does not establish clipboard privacy.
No producer is started until explicit policy, announcement and custody pass.
"""
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace, field
import hashlib
import os
from pathlib import Path
import selectors
import signal
import subprocess
import threading
import time

from tools.memoryweb_gate import ValidationContext, strict_load
from wender.act.actor import ActorLock
from wender.learn.mw_workflow import (
    WorkflowLimits, RunOwners, IdentityPlan, IdentityTransport, GroundedTarget,
    ObservationValue, PrivateSlot, _SlotLengthBinding, LocationFailure,
)
from wender.learn.mw_locate_ai import LocatePolicy, LocateAccounting, locate_once
from wender.learn.script_live import LegacyDispatchOwner
from wender.perceive.srf_identity import (
    SrfIdentitySurface, BINARY, CLI_MAPPING, load_identity_env,
)
from wender.scripts.types import AdmissionCheck, Truth, Reason


def _yes(value):
    return type(value) is AdmissionCheck and value.result is Truth.TRUE and value.reason is None


def _need(value):
    if not value:
        raise ValueError('OUT_OF_SUPPORT') from None


def _admit(value):
    _need(_yes(value))


_NO = AdmissionCheck(Truth.UNKNOWN, Reason.MISSING)


# @anchor:wa.learn.mw_owners_srf.LiveInvocation
@dataclass(frozen=True)
class LiveInvocation:
    workflow: Path
    output: Path
    mode: str
    identity: str
    container: str
    window: str
    identity_env: Path
    live_ack: str = field(repr=False)


# @anchor:wa.learn.mw_owners_srf.LivePorts
@dataclass(frozen=True)
class LivePorts:
    scope: object
    authority: object
    run_id: str
    limits: WorkflowLimits
    validation_context: ValidationContext
    artifact: object
    authorization: object
    capabilities: object
    observations: object
    ram: object
    slots: object
    resources: object
    journal: object
    replay_policy: object
    slot_length_bindings: tuple
    actor_lock: ActorLock
    actor_identity: str
    announce_gate: object
    clock: object
    sleep: object
    native_read: object
    private_paste: object


# @anchor:wa.learn.mw_owners_srf.build_live_ports
def build_live_ports(
    invocation, *, scope, authority, run_id, limits, validation_context,
    artifact, authorization, capabilities, observations, ram, slots,
    resources, journal, replay_policy, slot_length_bindings, actor_lock,
    actor_identity, announce_gate, clock, sleep, native_policy, private_paste,
):
    """Compose explicit trusted dependencies without probing or acquiring custody."""
    try:
        ports = LivePorts(scope, authority, run_id, limits, validation_context,
                          artifact, authorization, capabilities, observations, ram,
                          slots, resources, journal, replay_policy, slot_length_bindings,
                          actor_lock, actor_identity, announce_gate, clock, sleep,
                          BoundedNativeRead(native_policy), private_paste)
        _validate(invocation, ports)
        return ports
    except Exception:
        raise ValueError('OUT_OF_SUPPORT') from None


# @anchor:wa.learn.mw_owners_srf.NativeReadPolicy
@dataclass(frozen=True)
class NativeReadPolicy:
    """All limits are explicit trusted policy, including retained registrations."""
    stdout_bytes: int
    stderr_bytes: int
    rows: int
    string_bytes: int
    processes: int
    timeout_ns: int
    join_ns: int
    publications: int

    # @anchor:wa.learn.mw_owners_srf.NativeReadPolicy.__post_init__
    def __post_init__(self):
        _need(all(type(getattr(self, f.name)) is int and getattr(self, f.name) > 0
                  for f in fields(self)))


# @anchor:wa.learn.mw_owners_srf.BoundedNativeRead
class BoundedNativeRead:
    """One attempt, bounded concurrent pipe draining, private errors, joined child.

    A reader is invocation-local. The process factory is a trusted Python seam,
    never a workflow command. Reservations remain with the resource owner until
    that owner verifies drain. No raw producer diagnostics escape this boundary.
    """

    # @anchor:wa.learn.mw_owners_srf.BoundedNativeRead.__init__
    def __init__(self, policy, *, process_factory=subprocess.Popen):
        _need(type(policy) is NativeReadPolicy and callable(process_factory))
        self.policy, self._spawn = policy, process_factory
        self._mutex = threading.RLock()
        self._active = set()
        self._bound = False
        self._poisoned = False

    # @anchor:wa.learn.mw_owners_srf.BoundedNativeRead.bind
    def bind(self, invocation, *, resources, clock, deadline_ns):
        with self._mutex:
            _need(not self._bound and not self._active)
            self._invocation, self._resources = invocation, resources
            self._clock, self._deadline = clock, deadline_ns
            self._bound = True
        return self

    # @anchor:wa.learn.mw_owners_srf.BoundedNativeRead.read
    def read(self, operation, payload, deadline_ns):
        """Read only fixed native operations; private type and AX are refused."""
        try:
            return self._read(operation, payload, deadline_ns)
        except Exception:
            raise ValueError('OUT_OF_SUPPORT') from None

    def _read(self, operation, payload, deadline_ns):
        _need(self._bound and operation in ('frame', 'click', 'key', 'scroll', 'drag', 'downloads'))
        _need(type(payload) is dict)
        inv = self._invocation
        values = dict(capsule=inv.container, window=inv.window, **payload)
        if operation == 'downloads':
            _need(not payload)
            argv = [BINARY, 'downloads', '--container', inv.container]
        else:
            argv = [BINARY, *(word.format_map(values) for word in CLI_MAPPING[operation])]
            if operation in ('click', 'drag'):
                argv += ['--button', str(payload['button'])]
            if operation == 'scroll' and 'modifier' in payload:
                _need(payload['modifier'] in ('ctrl', 'shift'))
                argv += ['--modifier', payload['modifier']]
        remaining = min(deadline_ns, self._deadline, self._clock() + self.policy.timeout_ns) - self._clock()
        _need(type(remaining) is int and remaining > 0)
        # Reserve encoded buffers, Unicode and conservative JSON structural headroom before
        # invoking Popen. The trusted aggregate owner may reject this reservation.
        amount = 64 * (self.policy.stdout_bytes + self.policy.stderr_bytes)
        with self._mutex:
            _need(not self._poisoned and len(self._active) < self.policy.processes)
            _admit(self._resources.reserve('native_read', amount))
            # Environment file is the existing allowlisted identity configuration;
            # no workflow-supplied environment or inherited secrets are accepted.
            env = dict(load_identity_env(inv.identity_env), PATH=os.defpath)
            child = self._spawn(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, start_new_session=True)
            self._active.add(child)
        try:
            raw = self._collect(child, time.monotonic_ns() + remaining)
            value = strict_load(raw)
            _need(type(value) is (list if operation == 'downloads' else dict))
            if operation == 'frame':
                _ram_path(value.get('path'))
            return value
        except BaseException:
            raise ValueError('OUT_OF_SUPPORT') from None
        finally:
            self._join(child)

    def _collect(self, child, deadline):
        buffers = [bytearray(), bytearray()]
        caps = [self.policy.stdout_bytes, self.policy.stderr_bytes]
        with selectors.DefaultSelector() as selector:
            for index, stream in enumerate((child.stdout, child.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, index)
            while selector.get_map():
                _need(time.monotonic_ns() < deadline)
                for key, _ in selector.select(min((deadline - time.monotonic_ns()) / 1_000_000_000, 0.01)):
                    index = key.data
                    chunk = os.read(key.fileobj.fileno(), min(65536, caps[index] - len(buffers[index]) + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    _need(len(buffers[index]) + len(chunk) <= caps[index])
                    buffers[index].extend(chunk)
            # EOF does not imply process termination. Never block past deadline.
            child.wait(timeout=max(0, deadline - time.monotonic_ns()) / 1_000_000_000)
            _need(child.returncode == 0)
        return bytes(buffers[0])

    def _join(self, child):
        joined = False
        try:
            stream = getattr(child, 'stdin', None)
            if stream is not None:
                stream.close()
            if child.poll() is None:
                child.kill()
            # Kill descendants in our own session even when the leader exited.
            # Guard: a non-int (mocked) or <= 1 pid makes killpg act on every process (kill(-1)); never signal our own group.
            group = getattr(child, 'pid', None)
            group_ok = type(group) is int and group > 1 and group != os.getpgid(0)
            if group_ok:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            child.wait(timeout=self.policy.join_ns / 1_000_000_000)
            joined = child.poll() is not None
            if group_ok:
                try:
                    os.killpg(child.pid, 0)
                    joined = False
                except ProcessLookupError:
                    pass
        except Exception:
            joined = False
        finally:
            for stream in (getattr(child, 'stdin', None), child.stdout, child.stderr):
                if stream is not None:
                    stream.close()
            with self._mutex:
                if joined:
                    self._active.discard(child)
                else:
                    self._poisoned = True
        _need(joined)

    # @anchor:wa.learn.mw_owners_srf.BoundedNativeRead.drain
    def drain(self):
        with self._mutex:
            return AdmissionCheck(Truth.TRUE, None) if not self._active and not self._poisoned else _NO


# @anchor:wa.learn.mw_owners_srf._StagedPaste
@dataclass(repr=False)
class _StagedPaste:
    slot_id: str
    payload: bytearray = field(repr=False)
    deadline_ns: int


# @anchor:wa.learn.mw_owners_srf.StdinPrivatePaste
class StdinPrivatePaste(BoundedNativeRead):
    """One-use UTF-8 staging; parser evidence is bound to executable identity.

    The trusted slot owner still attests actual clipboard/history cleanup. This
    adapter erases only its own staging, and never retries uncertain input.
    """

    # @anchor:wa.learn.mw_owners_srf.StdinPrivatePaste.__init__
    def __init__(self, policy, *, resources, clock, deadline_ns,
                 process_factory=subprocess.Popen):
        super().__init__(policy, process_factory=process_factory)
        _need(callable(clock) and type(deadline_ns) is int and clock() < deadline_ns)
        _need(callable(getattr(resources, 'reserve', None)))
        self._resources, self._clock, self._deadline = resources, clock, deadline_ns
        self._pending = None
        self._support = None
        self._closed = False
        self._sending = False
        self._negotiating = False
        self._cohort = None

    # @anchor:wa.learn.mw_owners_srf.StdinPrivatePaste.bind
    def bind(self, invocation, *, scope, authority, surface, native_read=None):
        with self._mutex:
            _need(not self._bound and not self._closed and scope is not None and authority is not None)
            _need(type(invocation) is LiveInvocation and callable(getattr(surface, '_validated_input', None)))
            self._invocation, self._scope, self._authority = invocation, scope, authority
            self._surface = surface
            # Native reads, negotiation and sends share the actual process ceiling.
            if isinstance(native_read, BoundedNativeRead):
                _need(native_read.policy == self.policy and native_read._resources is self._resources)
                self._mutex, self._active = native_read._mutex, native_read._active
                self._cohort = native_read
            self._bound = True
        return self

    def _identity(self):
        st = os.stat(BINARY)
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)

    def _remaining(self, deadline_ns):
        _need(type(deadline_ns) is int)
        remaining = min(deadline_ns, self._deadline, self._clock() + self.policy.timeout_ns) - self._clock()
        _need(remaining > 0)
        return remaining

    def _execute(self, flags, payload, deadline_ns, expected_rc):
        remaining = self._remaining(deadline_ns)
        deadline = time.monotonic_ns() + remaining
        inv = self._invocation
        argv = [BINARY, 'input', '--container', inv.container, '--window', inv.window,
                *flags, '--timeout', str(remaining) + 'ns']
        amount = 64 * (self.policy.stdout_bytes + self.policy.stderr_bytes) + 2 * len(payload)
        with self._mutex:
            _need(not self._closed and not self._poisoned and len(self._active) < self.policy.processes)
            _admit(self._resources.reserve('private_pipe', amount))
            env = dict(load_identity_env(inv.identity_env), PATH=os.defpath)
            _need(time.monotonic_ns() < deadline and self._clock() < min(deadline_ns, self._deadline))
            _need(self._cohort is None or not self._cohort._poisoned)
            if '--paste' in flags:
                _need(self._identity() == self._support)
            child = self._spawn(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, start_new_session=True)
            self._active.add(child)
        try:
            raw, err = self._pump(child, payload, deadline)
            _need(child.returncode == expected_rc)
            if expected_rc == 2:
                _need(not err)
            return strict_load(raw)
        finally:
            self._join(child)

    def _pump(self, child, payload, deadline):
        buffers = [bytearray(), bytearray()]
        caps = [self.policy.stdout_bytes, self.policy.stderr_bytes]
        offset = 0
        with selectors.DefaultSelector() as selector:
            for index, stream in enumerate((child.stdout, child.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, index)
            if payload:
                os.set_blocking(child.stdin.fileno(), False)
                selector.register(child.stdin, selectors.EVENT_WRITE, 2)
            else:
                child.stdin.close()
            while selector.get_map():
                _need(time.monotonic_ns() < deadline)
                for key, _ in selector.select(max(0, min((deadline - time.monotonic_ns()) / 1_000_000_000, 0.01))):
                    index = key.data
                    if index == 2:
                        try:
                            wrote = os.write(key.fileobj.fileno(), memoryview(payload)[offset:offset + 4096])
                        except BlockingIOError:
                            continue
                        _need(wrote > 0)
                        offset += wrote
                        if offset == len(payload):
                            selector.unregister(key.fileobj)
                            child.stdin.close()
                    else:
                        try:
                            chunk = os.read(key.fileobj.fileno(), min(65536, caps[index] - len(buffers[index]) + 1))
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            _need(len(buffers[index]) + len(chunk) <= caps[index])
                            buffers[index].extend(chunk)
            child.wait(timeout=max(0, deadline - time.monotonic_ns()) / 1_000_000_000)
        return bytes(buffers[0]), bytes(buffers[1])

    # @anchor:wa.learn.mw_owners_srf.StdinPrivatePaste.check_ready
    def check_ready(self, invocation, scope, authority):
        probing = False
        try:
            with self._mutex:
                _need(self._bound and not self._closed and not self._poisoned and not self._negotiating)
                _need(invocation is self._invocation and scope == self._scope and authority == self._authority)
                _need(self._clock() < self._deadline)
                identity = self._identity()
                if self._support is not None:
                    _need(self._support == identity)
                    return AdmissionCheck(Truth.TRUE, None)
                self._negotiating = probing = True
            result = self._execute(['--type-stdin', '--key', 'Return'], b'', self._deadline, 2)
            _need(type(result) is dict and result == {
                        'error': {'code': 2, 'detail': 'choose exactly one input operation'}} and
                        type(result['error']['code']) is int)
            with self._mutex:
                _need(self._identity() == identity and not self._closed)
                self._support = identity
                return AdmissionCheck(Truth.TRUE, None)
        except Exception:
            return _NO
        finally:
            if probing:
                with self._mutex:
                    self._negotiating = False

    # @anchor:wa.learn.mw_owners_srf.StdinPrivatePaste.stage
    def stage(self, slot, payload, state):
        try:
            with self._mutex:
                _need(self._bound and not self._closed and not self._poisoned and not self._sending and self._pending is None)
                _need(type(payload) is bytes and len(payload) + 2 <= 16384)
                payload.decode('utf-8')   # invalid UTF-8 is refused locally, before the producer (judge W23 fix 2)
                _need(type(slot['slot_id']) is str and slot['transport'] == 'clipboard_paste')
                _need(type(state.deadline_ns) is int and type(slot['budget_ms']) is int and slot['budget_ms'] > 0)
                expiry = min(state.deadline_ns, self._deadline, self._clock() + slot['budget_ms'] * 1_000_000)
                _need(self._clock() < expiry)
                _admit(self._resources.reserve('private_stage', 2 * (len(payload) + 2)))
                self._pending = _StagedPaste(slot['slot_id'], bytearray(payload), expiry)
                return AdmissionCheck(Truth.TRUE, None)
        except Exception:
            raise ValueError('OUT_OF_SUPPORT') from None

    # @anchor:wa.learn.mw_owners_srf.StdinPrivatePaste.send
    def send(self, action, target):
        return self.send_at(action, target, deadline_ns=self._deadline)

    # @anchor:wa.learn.mw_owners_srf.StdinPrivatePaste.send_at
    def send_at(self, action, target, *, deadline_ns):
        owned = False
        try:
            with self._mutex:
                _need(not self._sending)
                pending = self._pending
                _need(pending is not None)
                _need(action['kind'] == 'paste' and target is None and pending.slot_id == action['text_slot'])
                self._sending = owned = True
                deadline = min(deadline_ns, pending.deadline_ns)
                self._remaining(deadline)
                # The producer strips one LF then one CR; framing preserves all original bytes.
                pending.payload.extend(b'\r\n')
            _admit(self.check_ready(self._invocation, self._scope, self._authority))
            result = self._execute(['--type-stdin', '--paste'], pending.payload, deadline, 0)
            return self._surface._validated_input(result, 'paste')['input_mono_ns']
        except Exception:
            raise ValueError('OUT_OF_SUPPORT') from None
        finally:
            with self._mutex:
                if owned or not self._sending:
                    self._erase()
                if owned:
                    self._sending = False

    def _erase(self):
        with self._mutex:
            if self._pending is not None:
                self._pending.payload[:] = b'\0' * len(self._pending.payload)
                self._pending = None

    # @anchor:wa.learn.mw_owners_srf.StdinPrivatePaste.clear
    def clear(self, slot):
        with self._mutex:
            _need(not self._sending)
            self._erase()
            return AdmissionCheck(Truth.TRUE, None)

    # @anchor:wa.learn.mw_owners_srf.StdinPrivatePaste.drain
    def drain(self):
        with self._mutex:
            self._closed = True
            if self._sending or self._negotiating:
                return _NO
            self._erase()
            return super().drain()


def _ram_path(path):
    _need(type(path) is str)
    source = Path(path)
    _need(source.is_absolute() and source.resolve().is_relative_to('/dev/shm') and
          not any(p.is_symlink() for p in (source, *source.parents)))


# @anchor:wa.learn.mw_owners_srf.DownloadsCodec
class DownloadsCodec:
    """Bounded run-local identities; same-name rewrites never create growth.

    The trusted downloads capability must attest finished entries. The native
    row schema has no completion field, so this codec does not infer completion.
    Names are private RAM keys; opaque identifiers are the only exported values.
    """

    # @anchor:wa.learn.mw_owners_srf.DownloadsCodec.__init__
    def __init__(self, policy, *, resources):
        _need(type(policy) is NativeReadPolicy)
        self._policy, self._resources = policy, resources
        self._names = {}
        self._salt = os.urandom(32)
        self._mutex = threading.RLock()

    # @anchor:wa.learn.mw_owners_srf.DownloadsCodec.decode
    def decode(self, rows):
        with self._mutex:
            _need(type(rows) is list and len(rows) <= self._policy.rows)
            names = set()
            for row in rows:
                _need(type(row) is dict and set(row) == {'name', 'bytes', 'mtime'})
                name = row['name']
                _need(type(name) is str and name and len(name) <= self._policy.string_bytes and len(name.encode('utf-8')) <= self._policy.string_bytes)
                _need(type(row['bytes']) is int and row['bytes'] >= 0 and type(row['mtime']) is int)
                _need(name not in names)
                names.add(name)
            new = names - self._names.keys()
            _need(len(self._names) + len(new) <= self._policy.rows)
            if new:
                _admit(self._resources.reserve('download_identities', sum(8 * len(n.encode('utf-8')) + 128 for n in new)))
                for name in new:
                    self._names[name] = hashlib.sha256(self._salt + name.encode('utf-8')).hexdigest()
            return frozenset(self._names[n] for n in names)


# @anchor:wa.learn.mw_owners_srf.DownloadsObservations
class DownloadsObservations:
    """Explicit trusted capability binding for native downloads, never AX.

    finished_gate(invocation, rows) must attest that the listing represents
    completed downloads in the authorized capsule. Completion policy is absent
    from the row schema, so a missing gate refuses before the native read.
    Registrations stay retained and charged until the enclosing owners drain.
    """

    # @anchor:wa.learn.mw_owners_srf.DownloadsObservations.__init__
    def __init__(self, invocation, *, ports, capability_id, revision, unit, finished_gate):
        _need(type(invocation) is LiveInvocation and type(ports) is LivePorts and callable(finished_gate))
        _need(all(type(v) is str and v and len(v.encode('utf-8')) <= ports.native_read.policy.string_bytes
                  for v in (capability_id, revision, unit)))
        self._invocation, self._ports = invocation, ports
        self._id, self._revision, self._unit = capability_id, revision, unit
        self._gate = finished_gate
        self._codec = DownloadsCodec(ports.native_read.policy, resources=ports.resources)
        self._registered = {}
        self._mutex = threading.RLock()

    # @anchor:wa.learn.mw_owners_srf.DownloadsObservations.acquire
    def acquire(self, predicate, deadline):
        p = self._ports
        with self._mutex:
            _need(type(predicate) is dict and predicate.get('capability_id') == self._id and
                  p.clock() < deadline <= p.limits.deadline_ns and
                  len(self._registered) < p.native_read.policy.publications)
            # Reserve publication and array/identity ownership before the read.
            _admit(p.resources.reserve('download_publication', 256))
            start = p.clock()
            rows = p.native_read.read('downloads', {}, deadline)
            _admit(self._gate(self._invocation, rows))
            ids = self._codec.decode(rows)
            end = p.clock()
            _need(start <= end < deadline)
            publication = 'downloads:' + str(len(self._registered))
            value = ObservationValue(scope=p.scope, authority=p.authority, publication=publication,
                capture_start_ns=start, capture_end_ns=end, capability_id=self._id,
                capability_revision=self._revision, output_kind='set', unit=self._unit,
                value=ids, complete=True, missing_reason=None)
            self._registered[publication] = value
            return value

    # @anchor:wa.learn.mw_owners_srf.DownloadsObservations.validate
    def validate(self, value):
        p = self._ports
        with self._mutex:
            valid = (type(value) is ObservationValue and self._registered.get(value.publication) is value and
                     value.scope == p.scope and value.authority == p.authority and value.complete is True and
                     value.missing_reason is None and value.capability_id == self._id and
                     value.capability_revision == self._revision and value.output_kind == 'set' and
                     value.unit == self._unit and value.capture_end_ns <= p.clock() < p.limits.deadline_ns)
            return AdmissionCheck(Truth.TRUE, None) if valid else _NO


def _publication_bound(value, policy):
    if type(value) is not ObservationValue:
        return None
    metadata = (value.publication, value.capability_id, value.capability_revision, value.output_kind, value.unit)
    if not all(type(v) is str and 0 < len(v) <= policy.string_bytes for v in metadata):
        return None
    bound = 256 + sum(4 * len(v) for v in metadata)
    if value.output_kind == 'ram' and value.value is not None:
        # Retain an opaque custody-validated handle, never copy its asset here.
        # Encoded/decoded assets remain charged by the trusted RAM owner.
        return bound + 128
    if value.output_kind == 'boolean' and type(value.value) is bool:
        return bound
    if value.output_kind == 'integer' and type(value.value) is int and value.value.bit_length() <= 64:
        return bound + 64
    if (value.output_kind == 'set' and type(value.value) is frozenset and len(value.value) <= policy.rows and
            all(type(v) is str and len(v) <= policy.string_bytes for v in value.value)):
        return bound + sum(128 + 4 * len(v) for v in value.value)
    return None


class _Observations:
    # @anchor:wa.learn.mw_owners_srf._Observations.__init__
    def __init__(self, ports):
        self._ports = ports
        self._retained = {}
        self._mutex = threading.RLock()

    # @anchor:wa.learn.mw_owners_srf._Observations.acquire
    def acquire(self, predicate, deadline):
        p = self._ports
        _need(p.clock() < deadline <= p.limits.deadline_ns)
        value = p.observations.acquire(predicate, deadline)
        _need(self._valid(value) and value.capability_id == predicate['capability_id'])
        return self._register(value)

    def _register(self, value):
        p = self._ports
        _need(self._valid(value))
        with self._mutex:
            if self._retained.get(value.publication) is value:
                return value
            _need(len(self._retained) < p.native_read.policy.publications and
                  value.publication not in self._retained)
            _admit(p.resources.reserve('observation_registration', _publication_bound(value, p.native_read.policy)))
            self._retained[value.publication] = value
        return value

    def _valid(self, value):
        p = self._ports
        return (type(value) is ObservationValue and _publication_bound(value, p.native_read.policy) is not None and value.scope == p.scope and value.authority == p.authority and
                type(value.publication) is str and bool(value.publication) and value.complete is True and
                value.missing_reason is None and type(value.capture_start_ns) is int and
                type(value.capture_end_ns) is int and 0 <= value.capture_start_ns <= value.capture_end_ns <= p.clock() and
                _yes(p.observations.validate(value)))

    # @anchor:wa.learn.mw_owners_srf._Observations.validate
    def validate(self, value):
        return (AdmissionCheck(Truth.TRUE, None) if self._valid(value) and
                self._retained.get(value.publication) is value else _NO)


class _EvidenceLease:
    """Transfer successful evidence custody to the adapter until composition close.

    validate_policy must budget retained publications for the whole run. Failed
    proposals release immediately; accepted publications remain with their exact
    trusted owner, never converted into an admission manufactured by the adapter.
    """
    def __init__(self, owner):
        self._owner = owner
        self.accepted = None

    def __getattr__(self, name):
        return getattr(self._owner, name)

    # @anchor:wa.learn.mw_owners_srf._EvidenceLease.validate_target
    def validate_target(self, target, value, evidence, state):
        answer = self._owner.validate_target(target, value, evidence, state)
        if _yes(answer):
            self.accepted = evidence
        return answer

    # @anchor:wa.learn.mw_owners_srf._EvidenceLease.release
    def release(self, evidence, deadline):
        if self.accepted is evidence:
            # No physical release: authenticated custody is transferred to the
            # adapter registry, which revalidates it on every runner use.
            return evidence.custody_admission
        return self._owner.release(evidence, deadline)


# @anchor:wa.learn.mw_owners_srf._LocateRefused
class _LocateRefused(ValueError):
    # @anchor:wa.learn.mw_owners_srf._LocateRefused.__init__
    def __init__(self):
        super().__init__('OUT_OF_SUPPORT')


class _Locator:
    def __init__(self, policy, evidence_owner, transport, accounting, clock):
        _need(type(policy) is LocatePolicy and type(accounting) is LocateAccounting)
        self.policy, self.owner, self.transport = policy, evidence_owner, transport
        self.accounting, self.clock = accounting, clock
        self.targets = {}
        self._retained = {}
        self._closed = False
        self._lock = threading.RLock()

    # @anchor:wa.learn.mw_owners_srf._Locator._recover
    def _recover(self, target, state, deadline, capabilities):
        _need(self._lock.acquire(blocking=False))
        lease = _EvidenceLease(self.owner)
        try:
            _need(not self._closed)
            result = locate_once(target, step_state=state,
                policy=replace(self.policy, absolute_deadline_ns=min(self.policy.absolute_deadline_ns, deadline)),
                evidence_owner=lease, transport=self.transport, accounting=self.accounting, clock=self.clock)
            value = result.grounded_target
            if value is None:
                raise _LocateRefused()
            _need(type(value) is GroundedTarget and lease.accepted is not None)
            capabilities._observations._register(value.observation)
            wrapped = replace(value, support_owner=capabilities)
            self.targets[id(wrapped)] = (wrapped, value, lease.accepted, target, state)
            return wrapped
        finally:
            # Even registration failure must preserve accepted evidence custody.
            if lease.accepted is not None:
                self._retained[id(lease.accepted)] = lease.accepted
            self._lock.release()

    def _validate(self, target, value, state):
        with self._lock:
            row = self.targets.get(id(value))
            if row is None or row[0] is not value or row[3] != target or row[4] is not state:
                return _NO
            _, original, evidence, _, _ = row
            if self._closed or self.accounting.poisoned:
                return _NO
            return self.owner.validate_target(target, original, evidence, state)

    def _drain(self):
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if self._closed:
                return True
            start = self.clock()
            if not _yes(self.transport.cancel_and_drain(self.policy, self.clock)):
                return False
            if self.clock() > start + self.policy.cancel_ns + self.policy.drain_ns:
                return False
            retained = self.accounting.retained_evidence
            if retained is not None:
                self._retained[id(retained)] = retained
            for key, evidence in tuple(self._retained.items()):
                deadline = self.clock() + self.policy.drain_ns
                if not _yes(self.owner.release(evidence, deadline)) or self.clock() > deadline:
                    return False
                del self._retained[key]
                if self.accounting.retained_evidence is evidence:
                    self.accounting.retained_evidence = None
            self.targets.clear()
            self._closed = True
            return True
        except Exception:
            return False
        finally:
            self._lock.release()


class _Capabilities:
    # @anchor:wa.learn.mw_owners_srf._Capabilities.__init__
    def __init__(self, invocation, ports, observations, locator=None):
        self._invocation, self._ports = invocation, ports
        self._observations = observations
        self._locator = locator
        self.workflow = None
        self._state = None
        self._action_deadlines = {}

    def __getattr__(self, name):
        # Fixed installed Python owner only; never a JSON-selected symbol.
        return getattr(self._ports.capabilities, name)

    # @anchor:wa.learn.mw_owners_srf._Capabilities._bind_locator_run
    def _bind_locator_run(self, state):
        if self._locator is None:
            return
        p, accounting = self._ports, self._locator.accounting
        _need(state.scope == p.scope and state.authority == p.authority)
        _need(accounting._lock.acquire(blocking=False))
        try:
            if accounting.run is state:
                return
            _need(accounting.run is None and accounting.attempts == 0 and
                  accounting.known_tokens == 0 and not accounting.attempted_steps and
                  accounting.poisoned is False and accounting.retained_evidence is None and
                  accounting.usage_complete is True)
            accounting.run = state
        finally:
            accounting._lock.release()

    # @anchor:wa.learn.mw_owners_srf._Capabilities._locate_usage
    def _locate_usage(self, state):
        if self._locator is None:
            return None
        p, accounting = self._ports, self._locator.accounting
        _need(state.scope == p.scope and state.authority == p.authority)
        _need(accounting._lock.acquire(blocking=False))
        try:
            _need(accounting.run is state)
            snapshot = accounting.snapshot()
            _need(type(snapshot.model_calls) is int and snapshot.model_calls >= 0)
            _need(snapshot.tokens_used is None or
                  (type(snapshot.tokens_used) is int and snapshot.tokens_used >= 0))
            _need(type(snapshot.usage_complete) is bool)
            return (snapshot.model_calls, snapshot.tokens_used if snapshot.usage_complete else None)
        finally:
            accounting._lock.release()

    # @anchor:wa.learn.mw_owners_srf._Capabilities.supports
    def supports(self, workflow):
        p = self._ports
        for step in workflow['steps']:
            targets = (step.get('target'), step['action'].get('destination'))
            if any(type(target) is dict and target.get('rung') == 'T0' for target in targets):
                if p.ram is None or p.replay_policy is None:
                    return _NO
            verifiers = [step['verify']]
            while verifiers:
                spec = verifiers.pop()
                verifiers.extend(spec['fallback_verifiers'])
                if spec['mode'] in ('state_signal', 'side_effect'):
                    continue
                if spec['mode'] == 'after_crop' and not spec['mask_boxes_px']:
                    if p.ram is None or p.replay_policy is None:
                        return _NO
                elif not callable(getattr(p.capabilities, 'observe_verifier', None)):
                    return _NO
            action = step['action']
            if action['kind'] == 'scroll' and not callable(getattr(p.capabilities, 'plan', None)):
                return _NO
            if action['kind'] in ('click', 'right_click', 'drag') and (
                    type(action.get('button')) is not int or action['button'] not in (1, 2, 3) or
                    (action['kind'] == 'right_click' and action['button'] != 3)):
                return _NO
            if action['kind'] == 'double_click' or (action['kind'] != 'wait_signal' and action['path'] != 'coordinates'):
                return _NO
        if workflow['text_slots']:
            if p.private_paste is None or p.slots is None:
                return _NO
            if not all(callable(getattr(p.slots, n, None)) for n in ('admit', 'read', 'prepare', 'verify', 'clear', 'cleared')):
                return _NO
            if not all(callable(getattr(p.private_paste, n, None)) for n in ('check_ready', 'send')):
                return _NO
            if not _yes(p.private_paste.check_ready(self._invocation, p.scope, p.authority)):
                return _NO
            if any(s['transport'] != 'clipboard_paste' for s in workflow['text_slots']):
                return _NO
        answer = p.capabilities.supports(workflow)
        if _yes(answer):
            self.workflow = workflow
        return answer

    # @anchor:wa.learn.mw_owners_srf._Capabilities.target
    def target(self, target, state, deadline):
        self._state = state
        primary_refusal = None
        try:
            value = self._ports.capabilities.target(target, state, deadline)
        except LocationFailure as exc:
            if self._locator is None:
                raise
            primary_refusal = exc
            value = None
        if value is None and self._locator is not None:
            try:
                return self._locator._recover(target, state, deadline, self)
            except _LocateRefused:
                if primary_refusal is not None:
                    raise primary_refusal from None
                raise
        _need(type(value) is GroundedTarget and value.support_owner is self._ports.capabilities)
        _admit(self._ports.capabilities.validate_target(target, value, state))
        self._observations._register(value.observation)
        return replace(value, support_owner=self)

    # @anchor:wa.learn.mw_owners_srf._Capabilities.observe_verifier
    def observe_verifier(self, spec, state, deadline):
        self._state = state
        value = self._ports.capabilities.observe_verifier(spec, state, deadline)
        return self._observations._register(value)

    # @anchor:wa.learn.mw_owners_srf._Capabilities.validate_target
    def validate_target(self, target, value, state):
        _need(type(value) is GroundedTarget and value.support_owner is self)
        if self._locator is not None and self._locator._closed:
            return _NO
        if self._locator is not None and id(value) in self._locator.targets:
            if not _yes(self._observations.validate(value.observation)):
                return _NO
            return self._locator._validate(target, value, state)
        if self._locator is not None and any(
                e.observation.publication == value.observation.publication for e in self._locator._retained.values()):
            return _NO  # Copied/forged AI values cannot fall through to the ordinary owner.
        return self._ports.capabilities.validate_target(target, replace(value, support_owner=self._ports.capabilities), state)


    # @anchor:wa.learn.mw_owners_srf._Capabilities.focus
    def focus(self, step, state):
        self._state = state
        return self._ports.capabilities.focus(step, state)

    # @anchor:wa.learn.mw_owners_srf._Capabilities.action
    def action(self, action, target):
        p = self._ports
        start = p.clock()
        answer = p.capabilities.action(action, target)
        if _yes(answer):
            _need(type(action.get('delivery_budget_ms')) is int and action['delivery_budget_ms'] > 0)
            deadline = min(p.limits.deadline_ns, start + action['delivery_budget_ms'] * 1_000_000)
            if self._state is not None:
                _need(type(self._state.deadline_ns) is int)
                deadline = min(deadline, self._state.deadline_ns)
            if action['kind'] == 'paste':
                slot = next((s for s in self.workflow['text_slots'] if s['slot_id'] == action['text_slot']), None)
                _need(slot is not None)
                deadline = min(deadline, start + slot['budget_ms'] * 1_000_000)
            _need(p.clock() < deadline)
            _need(id(action) in self._action_deadlines or len(self._action_deadlines) < p.limits.steps)
            self._action_deadlines[id(action)] = deadline
        return answer


class _Ram:
    # @anchor:wa.learn.mw_owners_srf._Ram.__init__
    def __init__(self, owner, observations, capabilities):
        self._owner, self._observations = owner, observations
        self._capabilities = capabilities

    # @anchor:wa.learn.mw_owners_srf._Ram.t0
    def t0(self, target, instance, state, deadline):
        self._capabilities._state = state
        frame, descriptor, publication, admission = self._owner.t0(target, instance, state, deadline)
        _admit(admission)
        self._observations._register(publication)
        return frame, descriptor, publication, admission

    # @anchor:wa.learn.mw_owners_srf._Ram.change
    def change(self, spec, state, deadline):
        return self._owner.change(spec, state, deadline)


class _Slots:
    # @anchor:wa.learn.mw_owners_srf._Slots.__init__
    def __init__(self, ports):
        self._ports = ports
        self._owned = {}

    def __getattr__(self, name):
        return getattr(self._ports.slots, name)

    # @anchor:wa.learn.mw_owners_srf._Slots.read
    def read(self, slot, state, max_bytes):
        p = self._ports
        value = p.slots.read(slot, state, max_bytes)
        _need(type(value) is PrivateSlot and value.transport == 'owned_clipboard' and
              value.scope == p.scope and value.authority == p.authority and _yes(value.admission) and
              type(value.value) is str and len(value.value) <= min(max_bytes, p.limits.slot_bytes) and len(value.value.encode('utf-8')) <= min(max_bytes, p.limits.slot_bytes))
        return value

    # @anchor:wa.learn.mw_owners_srf._Slots.prepare
    def prepare(self, slot, payload, state):
        self._owned[slot['slot_id']] = slot
        try:
            answer = self._ports.slots.prepare(slot, payload, state)
            if type(self._ports.private_paste) is StdinPrivatePaste:
                _admit(answer)
                _admit(self._ports.private_paste.stage(slot, payload, state))
            return answer
        except Exception:
            if type(self._ports.private_paste) is StdinPrivatePaste:
                self._ports.private_paste.clear(slot)
            raise ValueError('OUT_OF_SUPPORT') from None

    # @anchor:wa.learn.mw_owners_srf._Slots.clear
    def clear(self, slot):
        try:
            return self._ports.slots.clear(slot)
        finally:
            if type(self._ports.private_paste) is StdinPrivatePaste:
                self._ports.private_paste.clear(slot)

    # @anchor:wa.learn.mw_owners_srf._Slots.cleared
    def cleared(self, slot):
        answer = self._ports.slots.cleared(slot)
        if _yes(answer):
            self._owned.pop(slot['slot_id'], None)
        return answer

    def _drain(self):
        clean = True
        for slot in tuple(self._owned.values()):
            try:
                clean = _yes(self.clear(slot)) and _yes(self.cleared(slot)) and clean
            except Exception:
                clean = False
        return clean


# @anchor:wa.learn.mw_owners_srf.SrfWorkflowTransport
class SrfWorkflowTransport:
    """Policy recheck followed by the frozen native geometry validator."""

    # @anchor:wa.learn.mw_owners_srf.SrfWorkflowTransport.__init__
    def __init__(self, surface, *, invocation, ports, capabilities):
        self.surface, self._invocation = surface, invocation
        self._ports, self._capabilities = ports, capabilities
        self._ordinary = IdentityTransport(surface=surface, planner=self._plan)
        self._local = threading.local()
        self._lock = threading.RLock()
        self._closed = False

    def _plan(self, action, target):
        kind = action['kind']
        _need(action['path'] == 'coordinates')
        if kind in ('click', 'right_click'):
            button = action.get('button')
            _need(type(button) is int and button in (1, 2, 3) and (kind != 'right_click' or button == 3))
            return IdentityPlan('click', {'point': ','.join(map(str, target.point)), 'button': button}, AdmissionCheck(Truth.TRUE, None))
        if kind == 'key':
            _need(type(action.get('key_name')) is str and bool(action['key_name']))
            return IdentityPlan('key', {'keysym': action['key_name']}, AdmissionCheck(Truth.TRUE, None))
        if kind == 'scroll':
            # The frozen workflow has no native direction/modifier fields. Only
            # a registered trusted profile can provide their producer semantics.
            plan = self._ports.capabilities.plan(action, target)
            _need(type(plan) is IdentityPlan and plan.operation == 'scroll' and _yes(plan.admission))
            _need(type(plan.payload) is dict and plan.payload.get('dir') in ('up', 'down', 'left', 'right'))
            _need(plan.payload.get('modifier') in (None, 'ctrl', 'shift'))
            return plan
        if kind == 'drag':
            endpoint = target.destination
            displacement = type(action.get('dx_px')) is int and type(action.get('dy_px')) is int
            _need((action.get('destination') is not None) != displacement)
            if endpoint is None:
                _need(displacement)
                endpoint = (target.point[0] + action['dx_px'], target.point[1] + action['dy_px'])
            elif displacement:
                _need(endpoint == (target.point[0] + action['dx_px'], target.point[1] + action['dy_px']))
            button = action.get('button')
            _need(type(button) is int and button in (1, 2, 3))
            return IdentityPlan('drag', {'span': ','.join(map(str, target.point)) + ':' + ','.join(map(str, endpoint)),
                                         'button': button}, AdmissionCheck(Truth.TRUE, None))
        raise ValueError('OUT_OF_SUPPORT')

    # @anchor:wa.learn.mw_owners_srf.SrfWorkflowTransport.send
    def send(self, action, target):
        with self._lock:
            _need(not self._closed)
            try:
                p = self._ports
                _need(type(action.get('delivery_budget_ms')) is int and action['delivery_budget_ms'] > 0)
                self._local.deadline = min(p.limits.deadline_ns, p.clock() + action['delivery_budget_ms'] * 1_000_000,
                                          self._capabilities._action_deadlines.pop(id(action), p.limits.deadline_ns))
                _need(p.clock() < self._local.deadline)
                return self._send(action, target)
            except Exception:
                raise ValueError('OUT_OF_SUPPORT') from None
            finally:
                self._local.deadline = None

    def _send(self, action, target):
        p = self._ports
        _need(self._capabilities.workflow is not None and p.clock() < p.limits.deadline_ns)
        _admit(p.authorization.check('send', self._capabilities.workflow, p.scope, p.authority))
        _admit(p.capabilities.action(action, target))
        if action['kind'] == 'paste':
            _need(p.private_paste is not None)
            _admit(p.private_paste.check_ready(self._invocation, p.scope, p.authority))
            _admit(p.authorization.check('send', self._capabilities.workflow, p.scope, p.authority))
            if type(p.private_paste) is StdinPrivatePaste:
                return p.private_paste.send_at(action, target, deadline_ns=self._local.deadline)
            return p.private_paste.send(action, target)
        return self._ordinary.send(action, target)


    # @anchor:wa.learn.mw_owners_srf.SrfWorkflowTransport.close
    def close(self):
        # Refuse all subsequent public sends, including callers retaining owners
        # beyond the context. A timed-out in-flight send keeps actor custody.
        if not self._lock.acquire(timeout=self._ports.limits.drain_ns / 1_000_000_000):
            self._closed = True
            return False
        try:
            self._closed = True
            return True
        finally:
            self._lock.release()


# Canonical process-local exclusion supplements, never replaces, the trusted
# cross-process capsule lease in authorization and announcement admission.
_CANONICAL = {}
_LOCATOR_CUSTODY = {}
_CANONICAL_MUTEX = threading.RLock()


def _validate(invocation, ports):
    _need(type(invocation) is LiveInvocation and type(ports) is LivePorts)
    _need(all(isinstance(getattr(invocation, n), Path) for n in ('workflow', 'output', 'identity_env')))
    _need(invocation.mode in ('replay', 'shadow') and type(invocation.live_ack) is str and bool(invocation.live_ack.strip()))
    _need(all(type(getattr(invocation, n)) is str and bool(getattr(invocation, n).strip()) and
              not getattr(invocation, n).startswith('-') for n in ('identity', 'container', 'window')))
    _need(type(ports.limits) is WorkflowLimits and all(type(getattr(ports.limits, f.name)) is int and
          getattr(ports.limits, f.name) > 0 for f in fields(ports.limits)))
    _need(ports.limits.workflow_bytes <= 65536 and callable(ports.clock) and callable(ports.sleep))
    _need(type(ports.clock()) is int and ports.clock() < ports.limits.deadline_ns)
    _need(ports.scope is not None and ports.authority is not None and type(ports.run_id) is str and bool(ports.run_id))
    _need(type(ports.validation_context) is ValidationContext and isinstance(ports.actor_lock, ActorLock))
    _need(type(ports.actor_identity) is str and bool(ports.actor_identity.strip()) and callable(ports.announce_gate))
    methods = {
        'artifact': ('pin', 'check'), 'authorization': ('inventory', 'check'),
        'capabilities': ('supports', 'target', 'validate_target', 'focus', 'action', 'verifier', 'equivalent', 'satisfaction', 'final', 'changed'),
        'observations': ('acquire', 'validate'), 'resources': ('reserve', 'cancel', 'drain'),
        'journal': ('read', 'digest', 'authenticate', 'recover', 'reserve', 'append', 'stage', 'validate', 'select', 'context'),
        'native_read': ('bind', 'read', 'drain'),
    }
    _need(all(all(callable(getattr(getattr(ports, name), m, None)) for m in names) for name, names in methods.items()))
    _need(type(ports.native_read.policy) is NativeReadPolicy)
    _need(type(ports.slot_length_bindings) is tuple and all(type(b) is _SlotLengthBinding for b in ports.slot_length_bindings))


# @anchor:wa.learn.mw_owners_srf.open_srf_owners
@contextmanager
def open_srf_owners(invocation, *, ports, surface_factory=SrfIdentitySurface):
    """Yield exact frozen owners; retain custody if any cleanup is unverified."""
    with _open_srf_owners(invocation, ports=ports, surface_factory=surface_factory, locator=None) as owners:
        yield owners


# @anchor:wa.learn.mw_owners_srf.open_srf_owners_with_locator
@contextmanager
def open_srf_owners_with_locator(invocation, *, ports, locator_policy, evidence_owner,
                                 locate_transport, accounting, surface_factory=SrfIdentitySurface):
    """Explicit trusted embedding API; frozen LivePorts and shell stay unchanged.

    The evidence owner must budget retained successful publications through close.
    Receipt accounting binds to the runner through private capability hooks.
    T0 recovery remains outside this composition.
    """
    locator = _Locator(locator_policy, evidence_owner, locate_transport, accounting, ports.clock)
    with _open_srf_owners(invocation, ports=ports, surface_factory=surface_factory, locator=locator) as owners:
        yield owners


@contextmanager
def _open_srf_owners(invocation, *, ports, surface_factory, locator):
    try:
        _validate(invocation, ports)
        _admit(ports.announce_gate(invocation, scope=ports.scope, authority=ports.authority,
                                 now_ns=ports.clock(), absolute_deadline_ns=ports.limits.deadline_ns))
        _need(ports.clock() < ports.limits.deadline_ns)
    except Exception:
        raise ValueError('OUT_OF_SUPPORT') from None
    key = (invocation.container, invocation.window)
    with _CANONICAL_MUTEX:
        _need(key not in _CANONICAL)
        ports.actor_lock.acquire(ports.actor_identity)
        _CANONICAL[key] = ports.actor_lock
        if locator is not None:
            _LOCATOR_CUSTODY[key] = locator
    surface, dispatch, slots, transport = None, None, None, None
    try:
        native = ports.native_read.bind(invocation, resources=ports.resources, clock=ports.clock,
                                       deadline_ns=ports.limits.deadline_ns)
        def bounded(operation, payload):
            # This composition deliberately installs no AX/text or raw type port.
            _need(operation in ('frame', 'click', 'key', 'scroll', 'drag'))
            if operation != 'frame':
                _admit(ports.authorization.check('send', capabilities.workflow, ports.scope, ports.authority))
            deadline = ports.limits.deadline_ns if operation == 'frame' else transport._local.deadline
            _need(type(deadline) is int and ports.clock() < deadline)
            return native.read(operation, payload, deadline)
        surface = surface_factory(identity=invocation.identity, container=invocation.container,
                                  window=invocation.window, identity_env=invocation.identity_env,
                                  timeout=min(ports.native_read.policy.timeout_ns,
                                              ports.limits.deadline_ns - ports.clock()) / 1_000_000_000,
                                  transport=bounded, trace=None)
        if type(ports.private_paste) is StdinPrivatePaste:
            ports.private_paste.bind(invocation, scope=ports.scope, authority=ports.authority,
                                     surface=surface, native_read=native)
        surface.start()
        dispatch = LegacyDispatchOwner(surface)
        observations = _Observations(ports)
        capabilities = _Capabilities(invocation, ports, observations, locator)
        slots = _Slots(ports) if ports.slots is not None else None
        transport = SrfWorkflowTransport(surface, invocation=invocation, ports=ports, capabilities=capabilities)
        owners = RunOwners(artifact=ports.artifact, validation_context=ports.validation_context,
                           authorization=ports.authorization, capabilities=capabilities,
                           observations=observations, ram=_Ram(ports.ram, observations, capabilities) if ports.ram is not None else None,
                           transport=transport,
                           slots=slots, resources=ports.resources, journal=ports.journal, dispatch=dispatch,
                           surface=surface, replay_policy=ports.replay_policy, clock=ports.clock, sleep=ports.sleep,
                           limits=ports.limits, scope=ports.scope, authority=ports.authority, run_id=ports.run_id,
                           mode=invocation.mode, output=invocation.output, slot_length_bindings=ports.slot_length_bindings)
        yield owners
    except Exception:
        raise ValueError('OUT_OF_SUPPORT') from None
    finally:
        # Failed locator cleanup retains actor, surface and resource custody.
        if locator is not None:
            _need(locator._drain())
        clean = True
        for operation in (
            (lambda: transport.close()) if transport is not None else (lambda: True),
            (lambda: dispatch.close()) if dispatch is not None else (lambda: True),
            lambda: _yes(ports.resources.cancel(ports.limits.cancel_ns)),
            (lambda: slots._drain()) if slots is not None else (lambda: True),
            lambda: _yes(ports.native_read.drain()),
            (lambda: _yes(ports.private_paste.drain())) if type(ports.private_paste) is StdinPrivatePaste else (lambda: True),
        ):
            try:
                clean = operation() is True and clean
            except BaseException:
                clean = False
        if clean:
            try:
                clean = _yes(ports.resources.drain(ports.limits.drain_ns))
            except BaseException:
                clean = False
        if clean:
            try:
                if surface is not None:
                    surface.stop()
            except BaseException:
                clean = False
        if clean:
            with _CANONICAL_MUTEX:
                ports.actor_lock.release(ports.actor_identity)
                del _CANONICAL[key]
                _LOCATOR_CUSTODY.pop(key, None)
        _need(clean)
